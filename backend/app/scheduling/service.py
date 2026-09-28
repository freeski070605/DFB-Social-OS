from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import Brand, Content, Job
from app.schemas.domain import BrandConfig
from app.repositories.common import require
from app.content.service import transition, similarity
from app.db.session import utcnow
from app.core.errors import DomainError
from app.audit.service import record


def utc(value):
    if value.tzinfo is None:
        raise DomainError("Schedule timestamps must include a timezone")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def slots(config, start=None):
    now = (start or utcnow()).replace(tzinfo=timezone.utc)
    local = now.astimezone(ZoneInfo(config.schedule.timezone))
    result = []
    for offset in range(config.schedule.buffer_days + 1):
        date = local.date() + timedelta(days=offset)
        if date.weekday() not in config.schedule.weekdays:
            continue
        for hhmm in config.schedule.times:
            value = datetime.fromisoformat(f"{date.isoformat()}T{hhmm}").replace(tzinfo=ZoneInfo(config.schedule.timezone))
            candidate = value.astimezone(timezone.utc)
            # Reject nonexistent spring-forward wall times.
            if candidate.astimezone(ZoneInfo(config.schedule.timezone)).strftime("%H:%M") != hhmm:
                continue
            if now < candidate <= now + timedelta(days=config.schedule.buffer_days):
                result.append(candidate.replace(tzinfo=None))
    return sorted(set(result))


def schedule(db, brand_id, content_id, run_at, actor="admin", override=False, reason=""):
    item = require(db, Content, content_id, brand_id)
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    when = utc(run_at)
    if when <= utcnow():
        raise DomainError("Choose a future publish time")
    if item.status not in {"APPROVED", "SCHEDULED"}:
        raise DomainError("Approve content before scheduling", 409)
    local = when.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.schedule.timezone))
    minutes = local.hour * 60 + local.minute
    within = local.weekday() in config.schedule.weekdays and any(0 <= minutes - (int(t[:2]) * 60 + int(t[3:])) <= config.schedule.window_minutes for t in config.schedule.times)
    if not within and not (override and reason):
        raise DomainError("Outside the brand posting window. Provide an explicit override and reason.")
    history = db.scalars(select(Content).where(Content.brand_id == brand_id, Content.id != item.id,
        Content.status.in_(["PUBLISHED", "SCHEDULED"]))).all()
    for other in history:
        previous = other.scheduled_at or other.published_at
        if previous and abs((when - previous).total_seconds()) < config.schedule.min_repeat_days * 86400:
            if (other.parent_id or other.id) == (item.parent_id or item.id) or similarity(item.topic, other.topic) > .85:
                if set(item.targets) & set(other.targets):
                    raise DomainError("A similar topic or variant targets the same audience inside the repetition window")
    key = f"publish:{item.id}:r{item.revision}"
    job = db.scalar(select(Job).where(Job.key == key))
    if job and job.status in {"RUNNING", "UNKNOWN", "DONE"}:
        raise DomainError("This revision has already run or needs reconciliation", 409)
    if not job:
        job = Job(brand_id=brand_id, kind="publish", target_id=item.id, key=key)
        db.add(job)
    job.run_at, job.status, job.error = when, "PENDING", ""
    job.payload = {"revision": item.revision}
    if item.status != "SCHEDULED":
        transition(db, item, "SCHEDULED", actor)
    item.scheduled_at = when
    record(db, "content.schedule", item.id, brand_id, actor, after={"run_at": when.isoformat()}, reason=reason)
    return job


def job_action(db, brand_id, job_id, action, actor):
    job = require(db, Job, job_id, brand_id)
    if job.status in {"RUNNING", "UNKNOWN", "DONE"}:
        raise DomainError("Running, unknown and completed jobs cannot be restarted blindly", 409)
    if action == "cancel":
        job.status = "CANCELLED"
        if job.kind == "publish":
            item = require(db, Content, job.target_id, brand_id)
            if item.status == "SCHEDULED":
                transition(db, item, "APPROVED", actor)
    elif action == "pause":
        job.status = "PAUSED"
    elif action == "resume":
        job.status, job.run_at, job.error = "PENDING", utcnow(), ""
    else:
        raise DomainError("Unknown job action")
    record(db, "job." + action, job.id, brand_id, actor)
    return job
