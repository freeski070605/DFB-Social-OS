from collections import Counter
from datetime import timezone
from sqlalchemy import select
from app.models import Brand, Content, Knowledge, Job
from app.schemas.domain import BrandConfig, GenerationInput
from app.content.service import generate, similarity
from app.creative.service import render_content
from app.scheduling.service import slots, schedule
from app.approvals.policy import permit
from app.repositories.common import require
from app.audit.service import record


def maintain(db, brand_id):
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    if not brand.enabled or not config.pillars:
        return {"created": 0, "reason": "Brand disabled or no pillars configured"}
    calendar = slots(config)
    items = db.scalars(select(Content).where(Content.brand_id == brand_id).order_by(Content.id.desc())).all()
    occupied = {i.scheduled_at for i in items if i.status == "SCHEDULED"}
    free = [s for s in calendar if s not in occupied]
    ready = [i for i in reversed(items) if i.status == "APPROVED"]
    for item, when in zip(ready, free):
        payload = {"run_at": when.replace(tzinfo=timezone.utc).isoformat(), "revision": item.revision}
        if permit(db, brand, "schedule", item.id, payload):
            schedule(db, brand_id, item.id, when.replace(tzinfo=timezone.utc), "system")
    pending = [i for i in items if i.status in {"DRAFT", "REVIEW", "APPROVED", "SCHEDULED"}]
    deficit = max(0, len(calendar) - len(pending))
    counts = Counter(i.pillar for i in items[:40] if i.status != "ARCHIVED")
    weights = config.strategy.get("pillar_weights", {})
    pillars = sorted(config.pillars, key=lambda p: counts[p] / max(.8, min(1.2, weights.get(p, 1))))
    knowledge = db.scalars(select(Knowledge).where(Knowledge.brand_id == brand_id, Knowledge.enabled.is_(True), Knowledge.verification == "APPROVED")).all()
    candidates = sorted(knowledge, key=lambda k: pillars.index(k.category) if k.category in pillars else len(pillars))
    created = 0
    for source in candidates:
        if created >= min(deficit, 3):  # Bounded local-model work per director run.
            break
        if any(similarity(source.title, i.topic) > .8 for i in items):
            continue
        request = GenerationInput(topic=source.title, pillar=source.category if source.category in pillars else pillars[0],
                                  format=["carousel", "checklist", "steps"][created % 3])
        if not permit(db, brand, "generate", source.id, request.model_dump()):
            continue
        item = generate(db, brand_id, request, "system")
        items.append(item)
        if permit(db, brand, "render", item.id, {"revision": item.revision}):
            render_content(db, brand_id, item.id, "system")
        created += 1
        db.commit()
    record(db, "director.run", brand_id, brand_id, "system", after={"created": created, "deficit": deficit, "slots": len(calendar)})
    return {"created": created, "deficit": deficit, "slots": len(calendar), "note": "Add more approved knowledge when unique topics are exhausted"}


def enqueue(db, brand_id, kind, target_id=None, payload=None, key=None):
    from uuid import uuid4
    from app.db.session import utcnow
    require(db, Brand, brand_id)
    job = Job(brand_id=brand_id, kind=kind, target_id=target_id, payload=payload or {},
              key=key or f"{kind}:{uuid4().hex}", run_at=utcnow())
    db.add(job)
    db.flush()
    return job
