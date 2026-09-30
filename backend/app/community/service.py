import re
from datetime import timedelta
from sqlalchemy import select
from app.models import Interaction, Brand, PlatformAccount
from app.schemas.domain import BrandConfig, Classification
from app.ai.providers import provider
from app.repositories.common import require
from app.knowledge.service import search
from app.approvals.policy import outward_lock, outward_allowed, permit
from app.publishing.service import account_for
from app.publishing.providers import MetaPublisher
from app.core.errors import DomainError, ProviderError
from app.db.session import utcnow
from app.audit.service import record
from app.api.meta_auth import inspect_account

SENSITIVE = re.compile(r"\b(diagnos\w*|medic\w*|symptom\w*|pregnan\w*|suicid\w*|lawyer|legal|lawsuit|debt|invest\w*|loan|tax\w*|insurance|mortgage|sponsor\w*|collab\w*|partnership)\b", re.I)


def ingest(db, brand_id, data, actor="admin"):
    require(db, Brand, brand_id)
    existing = db.scalar(select(Interaction).where(Interaction.brand_id == brand_id, Interaction.platform == data.platform, Interaction.external_id == data.external_id))
    if existing:
        return existing
    item = Interaction(brand_id=brand_id, **data.model_dump())
    db.add(item)
    db.flush()
    record(db, "community.ingest", item.id, brand_id, actor)
    return item


def classify(db, brand_id, key):
    item, brand = require(db, Interaction, key, brand_id), require(db, Brand, brand_id)
    if item.status in {"REPLIED", "SENDING", "UNKNOWN", "CLOSED"} or item.taken_over:
        return item
    config = BrandConfig.model_validate(brand.config)
    refs = search(db, brand_id, item.body, approved=True, limit=4)
    classification = provider(config.ai).generate("Classify this interaction and draft a brief accurate reply. "
        "If uncertain or knowledge is insufficient, leave reply empty. Mark medical, legal, consequential financial advice as sensitive; business inquiries require a human.",
        {"brand": config.model_dump(), "interaction": item.body, "knowledge": [{"id": k.id, "body": k.body, "restrictions": k.restrictions} for k in refs]}, Classification)
    item.category, item.confidence, item.draft = classification.category, classification.confidence, classification.reply
    low_risk = (classification.category in {"positive", "simple_question"} and classification.category in config.interactions.auto_categories
                and classification.confidence >= config.interactions.min_confidence
                and not SENSITIVE.search(item.body + " " + item.draft)
                and not any(term.lower() in (item.body + " " + item.draft).lower() for term in config.interactions.blocked_terms)
                and (classification.category == "positive" or bool(refs)))
    item.action = "AUTO_REPLY" if low_risk and item.draft else "ESCALATE" if classification.category in {"sensitive", "business_inquiry", "collaboration"} else "ADMIN_REVIEW"
    item.status = "REVIEW"
    record(db, "community.classify", item.id, brand_id, "ai", after={"category": item.category, "action": item.action}, reason=classification.reason)
    if item.action == "AUTO_REPLY":
        action = "dm_reply" if item.kind == "dm" else "comment_reply"
        if permit(db, brand, action, item.id, {"body": item.draft}):
            from app.content.director import enqueue
            enqueue(db, brand_id, "reply", item.id, {"body": item.draft})
    return item


def reply(db, brand_id, key, body, actor="admin", automatic=False):
    item, brand = require(db, Interaction, key, brand_id), require(db, Brand, brand_id)
    if not body.strip() or len(body) > 1500:
        raise DomainError("Reply must be 1–1500 characters")
    if item.status in {"REPLIED", "SENDING", "UNKNOWN", "CLOSED"}:
        raise DomainError("Already replied or awaiting reconciliation", 409)
    config = BrandConfig.model_validate(brand.config)
    if automatic:
        action = "dm_reply" if item.kind == "dm" else "comment_reply"
        if config.permissions.get(action, "MANUAL") != "AUTO":
            raise DomainError("Automatic replies are not permitted for this brand", 409)
        if item.taken_over or item.action != "AUTO_REPLY" or SENSITIVE.search(item.body + " " + body):
            raise DomainError("This conversation requires a human", 409)
        if db.scalar(select(Interaction.id).where(Interaction.brand_id == brand_id, Interaction.thread_id == item.thread_id,
              Interaction.platform == item.platform, Interaction.taken_over.is_(True))) and item.thread_id:
            raise DomainError("Conversation is under human control", 409)
        recent = db.scalars(select(Interaction).where(Interaction.brand_id == brand_id, Interaction.replied_at > utcnow() - timedelta(hours=1))).all()
        if len(recent) >= config.interactions.max_replies_hour:
            raise ProviderError("Brand reply rate limit reached", transient=True)
        if any(r.draft.casefold() == body.casefold() or (r.thread_id and r.thread_id == item.thread_id) for r in recent):
            raise DomainError("Repetitive reply or reply loop prevented", 409)
    account = account_for(db, brand_id, item.platform)
    if not account:
        raise DomainError("No connected account; copy this draft and reply manually")
    own_ids = db.scalars(select(PlatformAccount.account_id).where(PlatformAccount.brand_id == brand_id)).all()
    if item.author in own_ids:
        raise DomainError("Replying to the brand itself is blocked")
    with outward_lock:
        outward_allowed(db, brand)
        inspect_account(account)
        if account.config.get("token_status") != "healthy":
            raise ProviderError("Meta account token is unhealthy. Reconnect before replying.")
        item.status, item.draft = "SENDING", body
        db.commit()
        try:
            item.reply_id = MetaPublisher().reply(account, item, body)
            item.status, item.replied_at = "REPLIED", utcnow()
            record(db, "community.reply", item.id, brand_id, actor, after={"reply_id": item.reply_id, "body": body},
                   details={"action_source": "AUTONOMOUS" if automatic else "MANUAL"})
            db.commit()
        except ProviderError as exc:
            item.status = "UNKNOWN" if exc.uncertain else "REVIEW"
            record(db, "community.reply_failed", item.id, brand_id, actor, result=item.status, reason=exc.message,
                   details={"action_source": "AUTONOMOUS" if automatic else "MANUAL"})
            db.commit()
            raise
    return item


def moderate(db, brand_id, key, action, actor):
    if action not in {"hide", "delete"}:
        raise DomainError("Blocking users must be handled by a human in the platform")
    item, brand = require(db, Interaction, key, brand_id), require(db, Brand, brand_id)
    account = account_for(db, brand_id, item.platform)
    if not account or item.kind != "comment":
        raise DomainError("A connected comment account is required")
    if item.status in {"SENDING", "UNKNOWN"}:
        raise DomainError("Moderation outcome must be reconciled before another action", 409)
    with outward_lock:
        outward_allowed(db, brand)
        inspect_account(account)
        if account.config.get("token_status") != "healthy":
            raise ProviderError("Meta account token is unhealthy. Reconnect before moderation.")
        item.status, item.action = "SENDING", "MODERATE_" + action.upper()
        record(db, "community." + action + ".intent", item.id, brand_id, actor)
        db.commit()
        try:
            MetaPublisher().moderate(account, item, action)
            item.status = "CLOSED"
            record(db, "community." + action, item.id, brand_id, actor)
        except ProviderError as exc:
            item.status = "UNKNOWN" if exc.uncertain else "REVIEW"
            record(db, "community." + action + ".failed", item.id, brand_id, actor, result=item.status, reason=exc.message)
            db.commit()
            raise
    return item


def takeover(db, brand_id, key, value, actor):
    item = require(db, Interaction, key, brand_id)
    group = db.scalars(select(Interaction).where(Interaction.brand_id == brand_id, Interaction.platform == item.platform, Interaction.thread_id == item.thread_id)).all() if item.thread_id else [item]
    for member in group:
        member.taken_over = value
    record(db, "community.takeover", item.id, brand_id, actor, after={"taken_over": value})
    return item
