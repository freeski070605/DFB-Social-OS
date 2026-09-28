from sqlalchemy import select
from app.models import Publication, Content, Brand, PlatformAccount
from app.db.session import utcnow
from app.repositories.common import require
from app.publishing.providers import MetaPublisher, ManualExportPublisher
from app.approvals.policy import outward_lock, outward_allowed
from app.audit.service import record
from app.core.errors import DomainError, ProviderError
from app.api.meta_auth import inspect_account


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


def publish(db, brand_id, content_id, actor="system"):
    item = require(db, Content, content_id, brand_id)
    if item.status == "PUBLISHED":
        return item
    if item.status not in {"APPROVED", "SCHEDULED", "PUBLISHING", "FAILED"}:
        raise DomainError("Content must be approved before publishing", 409)
    brand = require(db, Brand, brand_id)
    with outward_lock:
        outward_allowed(db, brand)
        for platform in dict.fromkeys(item.targets):
            pub = db.scalar(select(Publication).where(Publication.content_id == item.id, Publication.platform == platform))
            if not pub:
                pub = Publication(content_id=item.id, brand_id=brand_id, platform=platform)
                db.add(pub)
                db.flush()
            if pub.state in {"PUBLISHED", "EXPORTED"}:
                continue
            if pub.state in {"UNKNOWN", "SENDING"}:
                raise DomainError("Publication needs administrator reconciliation before any retry", 409)
            account = account_for(db, brand_id, platform) if platform != "manual" else None
            if platform != "manual" and not account:
                pub.state, pub.error = "CONFIGURATION", "No enabled account; use manual export or configure Meta"
                item.status = "APPROVED"
                db.commit()
                raise ProviderError(pub.error)
            if account:
                inspect_account(account)
                if account.config.get("token_status") != "healthy":
                    raise ProviderError("Meta account token is unhealthy. Reconnect before publishing.")
            item.status, pub.state, pub.attempts = "PUBLISHING", "SENDING", pub.attempts + 1
            db.commit()  # Intent is durable before any external request.
            def checkpoint(state):
                pub.request_state = state
                db.commit()
            try:
                outward_allowed(db, brand)
                external = ManualExportPublisher().publish(item) if platform == "manual" else MetaPublisher().publish(item, account, dict(pub.request_state), checkpoint)
                pub.external_id, pub.state, pub.error = external, "EXPORTED" if platform == "manual" else "PUBLISHED", ""
                record(db, "publication." + pub.state.lower(), item.id, brand_id, actor, after={"platform": platform, "external_id": external})
                db.commit()
            except ProviderError as exc:
                pub.state = "UNKNOWN" if exc.uncertain else "RETRY" if exc.transient else "CONFIGURATION"
                pub.error = exc.message
                item.status = "FAILED" if exc.uncertain or exc.transient else "APPROVED"
                record(db, "publication.error", item.id, brand_id, actor, result=pub.state, reason=exc.message)
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
        pub.request_state = {}
    item = require(db, Content, pub.content_id, brand_id)
    db.flush()
    pubs = db.scalars(select(Publication).where(Publication.content_id == item.id)).all()
    if all(p.state == "PUBLISHED" for p in pubs):
        item.status, item.published_at = "PUBLISHED", utcnow()
    else:
        item.status = "APPROVED"
    record(db, "publication.reconcile", pub.id, brand_id, actor, after={"state": pub.state, "external_id": external_id}, reason=reason)
    return pub
