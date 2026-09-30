import logging
from datetime import timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select, update
from app.db.session import SessionLocal, utcnow
from app.models import Job, Brand, Content, Publication, Interaction
from app.schemas.domain import BrandConfig, GenerationInput
from app.approvals.policy import permit, outward_allowed
from app.core.errors import ProviderError, DomainError
from app.audit.service import record

log = logging.getLogger("dfb.scheduler")


def recover():
    with SessionLocal() as db:
        for job in db.scalars(select(Job).where(Job.status == "RUNNING")):
            job.status = "UNKNOWN" if job.kind in {"publish", "reply"} else "PENDING"
            job.error = "Interrupted during previous run; inspect before retry" if job.status == "UNKNOWN" else "Recovered after restart"
        for pub in db.scalars(select(Publication).where(Publication.state == "SENDING")):
            pub.state, pub.error = "UNKNOWN", "Interrupted request; reconcile actual platform state"
            item = db.get(Content, pub.content_id)
            item.status = "FAILED"
        for item in db.scalars(select(Interaction).where(Interaction.status == "SENDING")):
            item.status = "UNKNOWN"
        db.commit()


def recurring(db):
    for brand in db.scalars(select(Brand).where(Brand.enabled.is_(True))):
        for kind, hours in (("director", 1), ("analytics", 6)):
            key = f"recurring:{kind}:{brand.id}"
            job = db.scalar(select(Job).where(Job.key == key))
            if not job:
                db.add(Job(brand_id=brand.id, kind=kind, key=key, run_at=utcnow(), payload={"interval_hours": hours}))
            elif job.status in {"DONE", "FAILED"}:
                job.status, job.run_at, job.attempts = "PENDING", utcnow() + timedelta(hours=hours), 0
    db.commit()


def execute(db, job):
    from app.content.director import maintain
    from app.content.service import generate
    from app.publishing.service import publish
    from app.community.service import classify, reply
    from app.analytics.service import collect
    brand = db.get(Brand, job.brand_id)
    if not brand:
        raise DomainError("Brand not found")
    if job.kind in {"publish", "reply"}:
        outward_allowed(db, brand)
    elif not brand.enabled:
        raise DomainError("Brand disabled")
    if job.kind == "publish":
        item = db.get(Content, job.target_id)
        if not item or item.brand_id != brand.id:
            raise DomainError("Scheduled content does not belong to this brand")
        mode = BrandConfig.model_validate(brand.config).permissions.get("publish", "MANUAL")
        if mode == "MANUAL" or (mode == "APPROVAL" and not job.payload.get("approved") and not permit(db, brand, "publish", item.id, {"job_id": job.id, "revision": item.revision})):
            job.status = "WAITING"
            return
        if item.revision != job.payload.get("revision"):
            raise DomainError("Content changed after scheduling")
        if utcnow() - job.run_at > timedelta(minutes=BrandConfig.model_validate(brand.config).schedule.window_minutes):
            job.status, job.error = "PAUSED", "Missed publishing window. Review and reschedule."
            return
        publish(db, brand.id, item.id)
    elif job.kind == "director":
        maintain(db, brand.id)
    elif job.kind == "generate":
        request = GenerationInput.model_validate({key: value for key, value in job.payload.items() if not key.startswith("_")})
        item = generate(db, brand.id, request, "system", job.payload.get("_editor_target_id"),
                        job.payload.get("_expected_revision"))
        job.target_id = item.id
    elif job.kind == "classify":
        classify(db, brand.id, job.target_id)
    elif job.kind == "reply":
        item = db.get(Interaction, job.target_id)
        if not item or item.brand_id != brand.id:
            raise DomainError("Interaction does not belong to this brand")
        action = "dm_reply" if item.kind == "dm" else "comment_reply"
        if not permit(db, brand, action, job.target_id, job.payload):
            job.status = "WAITING"
            return
        reply(db, brand.id, job.target_id, job.payload["body"], "system", automatic=True)
    elif job.kind == "analytics":
        collect(db, brand.id)
    else:
        raise DomainError("Unsupported job type")
    job.status = "DONE"
    if job.kind in {"publish", "reply"}:
        record(db, "job.done", job.id, brand.id, "system", details={"action_source": "AUTONOMOUS"})


def tick():
    try:
        with SessionLocal() as db:
            from app.publishing.service import cleanup_media
            cleanup_media(db)
            recurring(db)
            ids = list(db.scalars(select(Job.id).where(Job.status == "PENDING", Job.run_at <= utcnow()).order_by(Job.run_at).limit(8)))
            for key in ids:
                claimed = db.execute(update(Job).where(Job.id == key, Job.status == "PENDING").values(status="RUNNING", attempts=Job.attempts + 1))
                db.commit()
                if not claimed.rowcount:
                    continue
                job = db.get(Job, key, populate_existing=True)
                try:
                    execute(db, job)
                except DomainError as exc:
                    db.rollback()
                    job = db.get(Job, key, populate_existing=True)
                    job.error = exc.message
                    if exc.message in {"Outward actions paused", "Brand outward actions paused"}:
                        job.status, job.run_at = "PENDING", utcnow() + timedelta(seconds=30)
                        job.attempts = max(0, job.attempts - 1)
                    elif isinstance(exc, ProviderError) and exc.uncertain:
                        job.status = "UNKNOWN"
                    elif isinstance(exc, ProviderError) and exc.transient and job.attempts < 5:
                        job.status, job.run_at = "PENDING", utcnow() + timedelta(seconds=min(3600, 30 * 2 ** job.attempts))
                    else:
                        job.status = "FAILED"
                    record(db, "job.failure", key, job.brand_id, "system", result=job.status,
                           reason=exc.message, details={"action_source": "AUTONOMOUS"})
                except Exception:
                    db.rollback()
                    job = db.get(Job, key, populate_existing=True)
                    job.status, job.error = "UNKNOWN" if job.kind in {"publish", "reply"} else "FAILED", "Unexpected failure; inspect local logs"
                    log.exception("job_failure", extra={"job_id": key})
                    record(db, "job.failure", key, job.brand_id, "system", result=job.status,
                           reason=job.error, details={"action_source": "AUTONOMOUS"})
                db.commit()
    except Exception:
        log.exception("scheduler_tick_failed")


def start():
    recover()
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60})
    scheduler.add_job(tick, "interval", seconds=10, id="durable_queue_dispatch")
    scheduler.start()
    return scheduler
