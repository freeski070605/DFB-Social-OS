import re
from difflib import SequenceMatcher
from sqlalchemy import select
from app.models import Content, Brand, Knowledge, Job, Approval
from app.schemas.domain import ContentInput, GeneratedContent, BrandConfig
from app.repositories.common import require, serialize
from app.audit.service import record
from app.core.errors import DomainError
from app.knowledge.bulk import normalized
from app.ai.providers import provider

TRANSITIONS = {
    "IDEA": {"DRAFT", "ARCHIVED"}, "DRAFT": {"REVIEW", "ARCHIVED"},
    "REVIEW": {"DRAFT", "APPROVED", "ARCHIVED"}, "APPROVED": {"DRAFT", "SCHEDULED", "PUBLISHING", "ARCHIVED"},
    "SCHEDULED": {"APPROVED", "PUBLISHING", "ARCHIVED"}, "PUBLISHING": {"PUBLISHED", "FAILED", "APPROVED"},
    "FAILED": {"APPROVED", "DRAFT", "ARCHIVED"}, "PUBLISHED": {"ARCHIVED"}, "ARCHIVED": {"DRAFT"},
}


def canonical_pillar(db, brand_id, pillar):
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    match = next((name for name in config.pillars if normalized(name) == normalized(pillar)), None)
    if not match:
        raise DomainError("Pillar must match a configured pillar for this brand")
    return match


def generation_candidates(db, brand_id, topic, pillar, selected_ids=None, limit=8):
    brand = require(db, Brand, brand_id)
    if not brand.enabled:
        raise DomainError("Enable the brand before generating content")
    pillar = canonical_pillar(db, brand_id, pillar)
    rows = [item for item in db.scalars(select(Knowledge).where(
        Knowledge.brand_id == brand_id, Knowledge.verification == "APPROVED", Knowledge.enabled.is_(True)))
        if normalized(item.category) == normalized(pillar)]
    if selected_ids:
        by_id = {item.id: item for item in rows}
        if len(set(selected_ids)) != len(selected_ids) or any(key not in by_id for key in selected_ids):
            raise DomainError("Selected knowledge must be approved, enabled, and in this brand and pillar")
        return [by_id[key] for key in selected_ids]
    words = re.findall(r"\w{3,}", topic.casefold())[:12]
    rows.sort(key=lambda item: (-sum(word in item.title.casefold() for word in words) * 3 -
                                sum(word in item.body.casefold() for word in words), item.id))
    return rows[:limit]


def transition(db, item, state, actor="admin", reason=""):
    if state not in TRANSITIONS.get(item.status, set()):
        raise DomainError(f"Invalid transition: {item.status} → {state}", 409)
    if state in {"REVIEW", "APPROVED"}:
        quality = quality_check(db, item)
        if quality["errors"]:
            raise DomainError("; ".join(quality["errors"]))
        item.quality = quality
    before = item.status
    item.status = state
    if state in {"DRAFT", "ARCHIVED", "APPROVED"}:
        for job in db.scalars(select(Job).where(Job.target_id == item.id, Job.brand_id == item.brand_id, Job.kind == "publish", Job.status.in_(["PENDING", "WAITING", "PAUSED"]))):
            job.status = "CANCELLED"
        item.scheduled_at = None
    record(db, "content.transition", item.id, item.brand_id, actor, {"status": before}, {"status": state}, reason)
    return item


def quality_check(db, item):
    errors, warnings = [], []
    if not item.hook or not (item.body or item.slides or item.caption):
        errors.append("Content requires a hook and body, slides or caption")
    config = BrandConfig.model_validate(require(db, Brand, item.brand_id).config)
    text = " ".join([item.topic, item.hook, item.body, item.caption]).lower()
    for term in config.prohibited_topics:
        if term.lower() in text:
            warnings.append(f"Review prohibited topic: {term}")
    if not item.knowledge_refs:
        warnings.append("No approved knowledge attached; administrator must verify factual claims")
    if item.format in {"carousel", "checklist", "steps", "do_dont", "comparison", "single_graphic", "tip", "story"} and not item.slides:
        errors.append("This graphic format needs at least one slide")
    for number, slide in enumerate(item.slides or [], start=1):
        if not slide.get("title", "").strip():
            errors.append(f"Slide {number} title is required")
        kind = slide.get("kind")
        values = [value for value in slide.get("items", []) if value.strip()]
        if kind in {"checklist", "steps"} and not values and not slide.get("body", "").strip():
            errors.append(f"Slide {number} needs list items or explanation")
        if kind in {"two_column", "do_dont"} and len(values or [value for value in slide.get("body", "").split("\n\n") if value.strip()]) < 2:
            errors.append(f"Slide {number} needs at least two columns")
    if "instagram" in item.targets and not item.assets:
        errors.append("Render graphics before approving Instagram content")
    return {"score": max(0, 100 - 35 * len(errors) - 10 * len(warnings)), "status": "BLOCKED" if errors else "REVIEWED", "errors": errors, "warnings": warnings}


def save(db, brand_id, data: ContentInput, key=None, actor="admin"):
    data.pillar = canonical_pillar(db, brand_id, data.pillar)
    item = require(db, Content, key, brand_id) if key else Content(brand_id=brand_id)
    if key and item.status not in {"IDEA", "DRAFT", "REVIEW", "APPROVED", "FAILED", "ARCHIVED"}:
        raise DomainError("Unschedule content before editing. Published content is immutable; create a variant.", 409)
    if data.parent_id:
        require(db, Content, data.parent_id, brand_id)
    refs = []
    for ref_id in data.knowledge_refs:
        ref = require(db, Knowledge, ref_id, brand_id)
        if not ref.enabled or ref.verification != "APPROVED":
            raise DomainError("Only enabled, approved knowledge can be attached")
        refs.append(ref)
    before = {"revision": item.revision, "status": item.status} if key else {}
    for field, value in data.model_dump().items():
        setattr(item, field, value)
    item.sources = list(dict.fromkeys(data.sources + [ref.source for ref in refs if ref.source]))
    item.status, item.assets = "DRAFT", []
    item.revision = (item.revision or 0) + 1
    db.add(item)
    db.flush()
    item.quality = quality_check(db, item)
    for approval in db.scalars(select(Approval).where(Approval.brand_id == brand_id, Approval.target_id == item.id, Approval.action.in_(["publish", "schedule", "render"]), Approval.status == "PENDING")):
        approval.status = "SUPERSEDED"
    record(db, "content.edit" if key else "content.create", item.id, brand_id, actor, before, {"revision": item.revision, "topic": item.topic})
    return item


def similarity(a, b):
    a, b = re.sub(r"\W+", " ", a.lower()).strip(), re.sub(r"\W+", " ", b.lower()).strip()
    aa, bb = set(a.split()), set(b.split())
    return max(SequenceMatcher(None, a, b).ratio(), len(aa & bb) / max(1, len(aa | bb)))


def generate(db, brand_id, request, actor="admin"):
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    if not brand.enabled:
        raise DomainError("Enable the brand before generating content")
    parent = require(db, Content, request.parent_id, brand_id) if request.parent_id else None
    request.pillar = canonical_pillar(db, brand_id, request.pillar)
    refs = generation_candidates(db, brand_id, request.topic, request.pillar, request.knowledge_refs)
    if not refs:
        raise DomainError("Approve and enable knowledge for this pillar before AI generation")
    recent = db.scalars(select(Content).where(Content.brand_id == brand_id).order_by(Content.id.desc()).limit(100)).all()
    if not parent and any(similarity(request.topic, c.topic) > .85 for c in recent if c.status != "ARCHIVED"):
        raise DomainError("A similar topic already exists. Repurpose it or choose a different topic.", 409)
    output = provider(config.ai).generate(
        "Create useful, original content. Respect knowledge restrictions. Use the requested topic, pillar and format. "
        "Use short readable slides, cover first and end last. Cite only supplied knowledge IDs. "
        "Never invent sources. Do not repeat recent topics. Captions must avoid engagement bait.",
        {"brand": config.model_dump(), "request": request.model_dump(), "knowledge": [
            {"id": k.id, "title": k.title, "body": k.body[:8000], "source": k.source, "restrictions": k.restrictions} for k in refs],
         "recent_topics": [c.topic for c in recent[:30]], "parent": serialize(parent) if parent else None}, GeneratedContent)
    if set(output.knowledge_refs) - {k.id for k in refs} or not output.knowledge_refs:
        raise DomainError("Model output has missing or invalid knowledge provenance; regenerate")
    output.topic, output.pillar, output.format = request.topic, request.pillar, request.format
    output.parent_id, output.targets = request.parent_id, config.platforms
    output.sources = [k.source for k in refs if k.id in output.knowledge_refs and k.source]
    item = save(db, brand_id, ContentInput.model_validate(output.model_dump()), actor=actor)
    item.generation = {"provider": config.ai.provider, "model": config.ai.model, "knowledge": [
        {"id": k.id, "updated_at": k.updated_at.isoformat(), "body": k.body, "restrictions": k.restrictions} for k in refs if k.id in output.knowledge_refs]}
    transition(db, item, "REVIEW", actor)
    return item
