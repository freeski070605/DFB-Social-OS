from datetime import datetime
from app.models import Approval, Content, Job, StrategyChange
from app.repositories.common import require
from app.db.session import utcnow
from app.core.errors import DomainError
from app.audit.service import record


def decide(db, brand_id, key, approved, reason, actor):
    approval = require(db, Approval, key, brand_id)
    if approval.status != "PENDING":
        raise DomainError("Approval already decided", 409)
    action, target, payload = approval.action, approval.target_id, approval.payload
    if action in {"render", "schedule", "publish"}:
        item = require(db, Content, target, brand_id)
        if payload.get("revision", item.revision) != item.revision:
            raise DomainError("Content changed; this approval is stale", 409)
    approval.status, approval.reason, approval.decided_at = "APPROVED" if approved else "REJECTED", reason, utcnow()
    if approved:
        if action == "schedule":
            from app.scheduling.service import schedule
            schedule(db, brand_id, target, datetime.fromisoformat(payload["run_at"]), actor)
        elif action == "publish":
            job = require(db, Job, payload["job_id"], brand_id)
            job.payload = {**job.payload, "approved": True}
            job.status, job.run_at = "PENDING", utcnow()
        elif action == "render":
            from app.creative.service import render_content
            render_content(db, brand_id, target, actor)
        elif action == "generate":
            from app.content.director import enqueue
            enqueue(db, brand_id, "generate", payload=payload)
        elif action in {"comment_reply", "dm_reply"}:
            from app.community.service import reply
            # Persist the human decision before the external action.
            record(db, "approval.approved", key, brand_id, actor, reason=reason)
            db.commit()
            reply(db, brand_id, target, payload["body"], actor)
        elif action in {"hide", "delete"}:
            from app.community.service import moderate
            record(db, "approval.approved", key, brand_id, actor, reason=reason)
            db.commit()
            moderate(db, brand_id, target, action, actor)
        elif action == "strategy":
            from app.analytics.service import apply_strategy
            apply_strategy(db, brand_id, target, actor)
        else:
            raise DomainError("Unsupported approval action")
    else:
        if action == "publish":
            job = require(db, Job, payload["job_id"], brand_id)
            job.status = "CANCELLED"
        if action in {"schedule", "publish"}:
            from app.content.service import transition
            item = require(db, Content, target, brand_id)
            transition(db, item, "ARCHIVED", actor, reason)
        if action == "strategy":
            require(db, StrategyChange, target, brand_id).status = "REJECTED"
    record(db, "approval." + approval.status.lower(), key, brand_id, actor, reason=reason)
    return approval
