from datetime import datetime
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from app.db.session import get_db
from app.security.auth import authenticated
from app.models import Interaction, Approval, Job, Audit, StrategyChange
from app.repositories.common import require, serialize, list_brand
from app.schemas.domain import InteractionInput
from app.community import service as community
from app.approvals.service import decide
from app.content.director import enqueue
from app.scheduling.service import job_action, utc
from app.analytics import service as analytics
from app.audit.service import record

router = APIRouter(prefix="/api/brands/{brand_id}", tags=["operations"], dependencies=[Depends(authenticated)])


@router.get("/community")
def inbox(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, Interaction, brand_id, 1000)]


@router.post("/community")
def ingest(brand_id: int, data: InteractionInput, admin=Depends(authenticated), db=Depends(get_db)):
    item = community.ingest(db, brand_id, data, admin.username)
    db.commit()
    return serialize(item)


@router.post("/community/{key}/classify")
def classify(brand_id: int, key: int, db=Depends(get_db)):
    require(db, Interaction, key, brand_id)
    job = enqueue(db, brand_id, "classify", key)
    db.commit()
    return serialize(job)


class ReplyInput(BaseModel):
    body: str = Field(min_length=1, max_length=1500)


@router.post("/community/{key}/reply")
def reply(brand_id: int, key: int, data: ReplyInput, admin=Depends(authenticated), db=Depends(get_db)):
    return serialize(community.reply(db, brand_id, key, data.body, admin.username))


class CommunityAction(BaseModel):
    action: str
    reason: str = ""
    reply_id: str = ""
    body: str = Field(default="", max_length=1500)
    confirmed_absent: bool = False
    confirmed_applied: bool = False


@router.post("/community/{key}/action")
def community_action(brand_id: int, key: int, data: CommunityAction, admin=Depends(authenticated), db=Depends(get_db)):
    from app.core.errors import DomainError
    item = require(db, Interaction, key, brand_id)
    if data.action in {"takeover", "release"}:
        community.takeover(db, brand_id, key, data.action == "takeover", admin.username)
    elif data.action in {"hide", "delete"}:
        db.add(Approval(brand_id=brand_id, action=data.action, target_id=key, reason=data.reason))
    elif data.action == "close":
        if item.status in {"UNKNOWN", "SENDING"}:
            raise DomainError("Reconcile this interaction first")
        item.status = "CLOSED"
    elif data.action == "reconcile":
        if item.status != "UNKNOWN":
            raise DomainError("Only uncertain interactions can be reconciled", 409)
        if not data.reason.strip():
            raise DomainError("A reconciliation reason is required")
        if item.action.startswith("MODERATE_"):
            if data.confirmed_applied == data.confirmed_absent:
                raise DomainError("Confirm whether the moderation action happened")
            item.status = "CLOSED" if data.confirmed_applied else "REVIEW"
        else:
            if bool(data.reply_id) == data.confirmed_absent:
                raise DomainError("Supply the actual reply ID or explicitly confirm no reply was sent")
            item.status = "REPLIED" if data.reply_id else "REVIEW"
            item.reply_id = data.reply_id
    elif data.action == "manual_reply":
        if not data.reason.strip():
            raise DomainError("Describe where and when the reply was sent manually")
        if not data.body.strip():
            raise DomainError("Record the reply text that was sent manually")
        if item.status in {"SENDING", "UNKNOWN", "REPLIED", "CLOSED"}:
            raise DomainError("Reconcile or reopen this interaction before recording a manual reply", 409)
        item.status = "REPLIED"
        item.reply_id = data.reply_id
        item.draft = data.body
    else:
        raise DomainError("Unsupported action. Blocking users is performed in the platform.")
    record(db, "community." + data.action, key, brand_id, admin.username,
           after={"reply_id": item.reply_id, "body": item.draft} if data.action == "manual_reply" else None,
           reason=data.reason)
    db.commit()
    return serialize(item)


@router.get("/approvals")
def approvals(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, Approval, brand_id)]


class DecisionInput(BaseModel):
    approved: bool
    reason: str = ""


@router.post("/approvals/{key}")
def decision(brand_id: int, key: int, data: DecisionInput, admin=Depends(authenticated), db=Depends(get_db)):
    item = decide(db, brand_id, key, data.approved, data.reason, admin.username)
    db.commit()
    return serialize(item)


@router.get("/jobs")
def jobs(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, Job, brand_id, 500)]


@router.post("/jobs/{key}/{action}")
def change_job(brand_id: int, key: int, action: str, admin=Depends(authenticated), db=Depends(get_db)):
    job = job_action(db, brand_id, key, action, admin.username)
    db.commit()
    return serialize(job)


@router.get("/activity")
def activity(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, Audit, brand_id, 500)]


@router.get("/analytics")
def report(brand_id: int, days: int = 30, start: datetime | None = None, end: datetime | None = None, db=Depends(get_db)):
    return analytics.report(db, brand_id, min(3650, max(1, days)), utc(start) if start else None, utc(end) if end else None)


class SnapshotInput(BaseModel):
    content_id: int | None = None
    platform: str = "manual"
    metrics: dict[str, float]


@router.post("/analytics/snapshots")
def snapshot(brand_id: int, data: SnapshotInput, admin=Depends(authenticated), db=Depends(get_db)):
    item = analytics.add_snapshot(db, brand_id, data.content_id, data.platform, data.metrics, admin.username)
    db.commit()
    return serialize(item)


@router.post("/analytics/collect")
def collect(brand_id: int, db=Depends(get_db)):
    job = enqueue(db, brand_id, "analytics")
    db.commit()
    return serialize(job)


@router.get("/strategy")
def strategies(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, StrategyChange, brand_id)]


@router.post("/strategy")
def recommend(brand_id: int, db=Depends(get_db)):
    item = analytics.recommend(db, brand_id)
    db.commit()
    return serialize(item)


@router.post("/strategy/{key}/{action}")
def strategy_action(brand_id: int, key: int, action: str, admin=Depends(authenticated), db=Depends(get_db)):
    from app.core.errors import DomainError
    if action not in {"apply", "revert"}:
        raise DomainError("Unknown strategy action")
    item = analytics.apply_strategy(db, brand_id, key, admin.username, action == "revert")
    db.commit()
    return serialize(item)
