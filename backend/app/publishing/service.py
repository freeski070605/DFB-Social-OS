from sqlalchemy import select
from datetime import timedelta
import secrets
from app.models import Publication, Content, Brand, PlatformAccount, SystemSetting
from app.db.session import utcnow
from app.repositories.common import require
from app.publishing.providers import MetaPublisher, ManualExportPublisher, caption
from app.approvals.policy import outward_lock, outward_allowed, brand_outward_allowed
from app.audit.service import record
from app.core.errors import DomainError, ProviderError
from app.api.meta_auth import inspect_account
from app.publishing.public_media import r2_readiness_issue, r2_verified
from app.accounts.credentials import has_credential


def publishing_blockers(item, account, platform, db=None):
    reasons = []
    if not account:
        return ["No selected account for " + platform]
    config = account.config or {}
    if not has_credential(account):
        reasons.append("Selected account credential is unavailable")
    if config.get("token_status") != "healthy" or not config.get("last_checked"):
        reasons.append("Selected account credential has not passed a connection check")
    if config.get("expires_at") and config["expires_at"] <= utcnow().timestamp():
        reasons.append("Selected account credential has expired")
    if config.get("data_access_expires_at") and config["data_access_expires_at"] <= utcnow().timestamp():
        reasons.append("Selected account data access has expired")
    scopes = set(config.get("permissions") or [])
    required = {"pages_manage_posts"} if platform == "facebook" else {
        "pages_show_list", "pages_read_engagement", "instagram_basic", "instagram_content_publish"}
    for scope in sorted(required - scopes):
        reasons.append("Missing Meta permission: " + scope)
    if platform == "facebook" and config.get("tasks") and "CREATE_CONTENT" not in config["tasks"]:
        reasons.append("Selected Page lacks the CREATE_CONTENT task")
    if platform == "instagram" and not item.assets:
        reasons.append("Instagram requires rendered images")
    if platform == "instagram" and len(caption(item)) > 2200:
        reasons.append("Instagram caption, CTA and hashtags exceed 2200 characters")
    if platform == "facebook" and not item.assets and not (caption(item) or item.body).strip():
        reasons.append("Facebook text post is empty")
    if item.format in {"reel_script", "short_video_script", "story"}:
        reasons.append("This format is manual export only; Reel publishing is not implemented")
    if item.assets and (issue := r2_readiness_issue()):
        reasons.append(issue)
    elif item.assets and db is not None and not r2_verified(db):
        reasons.append("R2 public media has not passed a recent dry run")
    if len(item.assets) > 10 and platform == "instagram":
        reasons.append("Instagram supports at most 10 images in this integration")
    return reasons


def account_for(db, brand_id, platform):
    accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
        PlatformAccount.platform == platform, PlatformAccount.enabled.is_(True))).all()
    if len(accounts) > 1:
        raise DomainError("Multiple active Meta accounts. Select one in Settings before publishing.", 409)
    account = accounts[0] if accounts else None
    if account and db.scalar(select(PlatformAccount.id).where(PlatformAccount.platform == platform,
            PlatformAccount.account_id == account.account_id, PlatformAccount.brand_id != brand_id)):
        raise DomainError("This Meta account is assigned to another brand. Resolve the duplicate account before publishing.", 409)
    return account


def publish(db, brand_id, content_id, actor="system", platforms=None, account_ids=None,
            action_source="AUTONOMOUS"):
    if action_source not in {"AUTONOMOUS", "MANUAL_OWNER_CONFIRMED"}:
        raise DomainError("Invalid publishing action source")
    manual = action_source == "MANUAL_OWNER_CONFIRMED"
    item = require(db, Content, content_id, brand_id)
    if not manual and item.status == "PUBLISHED" and platforms is None:
        return item
    if item.status not in ({"APPROVED"} if manual else {"APPROVED", "SCHEDULED", "PUBLISHING", "FAILED", "PUBLISHED"}):
        raise DomainError("Content must be approved before publishing", 409)
    targets = list(dict.fromkeys(platforms if platforms is not None else item.targets))
    if not targets or any(platform not in {"facebook", "instagram", "manual"} for platform in targets):
        raise DomainError("Select a supported publishing destination")
    brand = require(db, Brand, brand_id)
    with outward_lock:
        gate = brand_outward_allowed if manual else outward_allowed
        gate(db, brand)
        if manual:
            from app.publishing.public_media import approved_asset
            for asset in item.assets:
                approved_asset(item, asset)
        for platform in targets:
            pub = db.scalar(select(Publication).where(Publication.content_id == item.id, Publication.platform == platform))
            if manual and pub and pub.state in {"PUBLISHED", "EXPORTED"}:
                raise DomainError("This destination already has a successful publication receipt", 409)
            if not pub:
                pub = Publication(content_id=item.id, brand_id=brand_id, platform=platform)
                db.add(pub)
                db.flush()
            if pub.state in {"PUBLISHED", "EXPORTED"}:
                continue
            if pub.state in {"UNKNOWN", "SENDING"}:
                raise DomainError("Publication needs administrator reconciliation before any retry", 409)
            account = account_for(db, brand_id, platform) if platform != "manual" else None
            if account_ids is not None and (account is None or account.id != account_ids.get(platform)):
                raise DomainError("Selected account changed; review the destination again", 409)
            if platform != "manual" and not account:
                pub.state, pub.error = "CONFIGURATION", "No enabled account; use manual export or configure Meta"
                item.status = "APPROVED"
                db.commit()
                raise ProviderError(pub.error)
            if account:
                inspect_account(account)
                blockers = publishing_blockers(item, account, platform, db)
                if blockers:
                    pub.state, pub.error = "CONFIGURATION", "; ".join(blockers)
                    item.status = "APPROVED"
                    db.commit()
                    raise ProviderError(pub.error)
            item.status, pub.state, pub.attempts = "PUBLISHING", "SENDING", pub.attempts + 1
            pub.request_state = {**(pub.request_state or {}), "idempotency_key": (pub.request_state or {}).get("idempotency_key") or secrets.token_hex(16)}
            record(db, "publication.intent", item.id, brand_id, actor,
                   details={"platform": platform, "action_source": action_source})
            db.commit()  # Intent is durable before any external request.
            def checkpoint(state):
                pub.request_state = state
                db.commit()
            try:
                gate(db, brand)
                external = ManualExportPublisher().publish(item) if platform == "manual" else MetaPublisher().publish(item, account, dict(pub.request_state), checkpoint)
                pub.external_id, pub.state, pub.error = external, "EXPORTED" if platform == "manual" else "PUBLISHED", ""
                if platform != "manual":
                    pub.request_state = {**pub.request_state, "cleanup_after": (utcnow() + timedelta(hours=24)).isoformat()}
                record(db, "publication." + pub.state.lower(), item.id, brand_id, actor,
                       after={"platform": platform, "external_id": external},
                       details={"action_source": action_source})
                db.commit()
            except ProviderError as exc:
                pub.state = "UNKNOWN" if exc.uncertain else "RETRY" if exc.transient else "CONFIGURATION"
                pub.error = exc.message
                if pub.state == "CONFIGURATION" and pub.request_state.get("media_objects"):
                    pub.request_state = {**pub.request_state, "cleanup_after": (utcnow() + timedelta(hours=24)).isoformat()}
                item.status = "FAILED" if exc.uncertain or exc.transient else "APPROVED"
                record(db, "publication.error", item.id, brand_id, actor, result=pub.state,
                       reason=exc.message, details={"platform": platform, "action_source": action_source})
                db.commit()
                raise
        states = db.scalars(select(Publication).where(Publication.content_id == item.id)).all()
        if all(p.state == "PUBLISHED" for p in states if p.platform != "manual") and any(p.platform != "manual" for p in states):
            item.status, item.published_at = "PUBLISHED", utcnow()
        else:
            item.status = "APPROVED"  # Export is not a publication.
        db.commit()
        return item


def reconcile(db, brand_id, publication_id, external_id, confirmed_absent, reason, actor):
    pub = require(db, Publication, publication_id, brand_id)
    if pub.state not in {"UNKNOWN", "SENDING", "CONFIGURATION", "RETRY", "EXPORTED"}:
        raise DomainError("Publication is not awaiting reconciliation")
    if not reason or (not external_id and not confirmed_absent):
        raise DomainError("Supply the actual external post ID or explicitly confirm absence, with a reason")
    pub.state, pub.error = ("PUBLISHED" if external_id else "READY"), ""
    pub.external_id = external_id
    if confirmed_absent:
        pub.request_state = {"media_objects": pub.request_state.get("media_objects", []),
                             "cleanup_after": (utcnow() + timedelta(hours=24)).isoformat()}
    elif external_id:
        pub.request_state = {**pub.request_state, "cleanup_after": (utcnow() + timedelta(hours=24)).isoformat()}
    item = require(db, Content, pub.content_id, brand_id)
    db.flush()
    pubs = db.scalars(select(Publication).where(Publication.content_id == item.id)).all()
    if all(p.state == "PUBLISHED" for p in pubs):
        item.status, item.published_at = "PUBLISHED", utcnow()
    else:
        item.status = "APPROVED"
    record(db, "publication.reconcile", pub.id, brand_id, actor, after={"state": pub.state, "external_id": external_id}, reason=reason)
    return pub


def cleanup_media(db):
    """Retry each deletion after a terminal result and a 24-hour fetch grace period."""
    from app.publishing.public_media import public_media_provider
    for pub in db.scalars(select(Publication).where(Publication.state.in_(["PUBLISHED", "CONFIGURATION", "READY"]))):
        state = pub.request_state or {}
        due = state.get("cleanup_after")
        if not due or due > utcnow().isoformat() or not state.get("media_objects"):
            continue
        for key in list(state["media_objects"]):
            try:
                public_media_provider().delete(key)
            except ProviderError:
                break
            state = {**state, "media_objects": [value for value in state["media_objects"] if value != key],
                     "deleted_objects": [*state.get("deleted_objects", []), key]}
            pub.request_state = state
            db.commit()
    for pending in db.scalars(select(SystemSetting).where(SystemSetting.key.like("dryrun_media:%"))):
        for key in list((pending.value or {}).get("objects", [])):
            try:
                public_media_provider().delete(key)
            except ProviderError:
                break
            pending.value = {"objects": [value for value in pending.value["objects"] if value != key]}
            db.commit()
        if not pending.value.get("objects"):
            db.delete(pending)
            db.commit()
    for plan in db.scalars(select(SystemSetting).where(SystemSetting.key.like("manual_publish_plan:%"))):
        if plan.value.get("expires_at", "") < utcnow().isoformat():
            db.delete(plan)
    db.commit()
