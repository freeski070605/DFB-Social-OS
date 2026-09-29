import re
from difflib import SequenceMatcher
from sqlalchemy import select
from app.models import Content, Brand, Knowledge, Job, Approval
from app.schemas.domain import ContentInput, GeneratedContent, BrandConfig, EditorialPlan, Slide
from app.repositories.common import require
from app.audit.service import record
from app.core.errors import DomainError
from app.knowledge.bulk import normalized
from app.ai.providers import provider
from app.content import editorial
from app.creative.renderer import render_slide

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
    words = set(re.findall(r"\w{3,}", topic.casefold())) - {
        "the", "and", "for", "you", "your", "things", "thing", "make", "with", "that", "this", "how", "what"}
    related = {"tonight": {"evening", "night", "nightly", "bedtime"},
               "tomorrow": {"morning", "next day"}}
    terms = words | set().union(*(related.get(word, set()) for word in words))

    def relevance(item):
        title, body = item.title.casefold(), item.body.casefold()
        return sum(3 * bool(re.search(r"\b" + re.escape(term) + r"\b", title)) +
                   bool(re.search(r"\b" + re.escape(term) + r"\b", body)) for term in terms)

    rows.sort(key=lambda item: (-relevance(item), item.id))
    return rows[:limit]


def transition(db, item, state, actor="admin", reason=""):
    if state not in TRANSITIONS.get(item.status, set()):
        raise DomainError(f"Invalid transition: {item.status} → {state}", 409)
    if state in {"REVIEW", "APPROVED"}:
        quality = quality_check(db, item, for_review=True)
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


def quality_check(db, item, for_review=False):
    errors, warnings = [], []
    substantive_slides = any(slide.get("body", "").strip() or any(value.strip() for value in slide.get("items", []))
                             for slide in item.slides or [])
    if not item.hook or not (item.body.strip() or substantive_slides or item.caption.strip()):
        errors.append("Content requires a hook and body, slide explanation/items or caption")
    config = BrandConfig.model_validate(require(db, Brand, item.brand_id).config)
    text = " ".join([item.topic, item.hook, item.body, item.caption]).lower()
    for term in config.prohibited_topics:
        if term.lower() in text:
            warnings.append(f"Review prohibited topic: {term}")
    if not item.knowledge_refs:
        warnings.append("No approved knowledge attached to this draft; select eligible records or verify factual claims")
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
    supporting_text = list(db.scalars(select(Knowledge.body).where(
        Knowledge.brand_id == item.brand_id, Knowledge.id.in_(item.knowledge_refs or []),
        Knowledge.verification == "APPROVED", Knowledge.enabled.is_(True))))
    assessment = editorial.evaluate(item.topic, item.slides or [], item.caption or "", item.knowledge_refs or [],
                                    config, evidence=supporting_text)
    return {"score": max(0, assessment["score"] - 25 * len(errors) - 5 * len(warnings)) if for_review else None,
            "status": ("BLOCKED" if errors else "REVIEWED") if for_review else "NOT_SCORED",
            "errors": errors, "warnings": warnings, "editorial": assessment}


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
        if not ref.enabled or ref.verification != "APPROVED" or normalized(ref.category) != normalized(data.pillar):
            raise DomainError("Only enabled, approved knowledge from the current pillar can be attached")
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


def generate(db, brand_id, request, actor="admin", target_id=None, expected_revision=None):
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    if not brand.enabled:
        raise DomainError("Enable the brand before generating content")
    parent = require(db, Content, request.parent_id, brand_id) if request.parent_id else None
    if target_id:
        if not parent or parent.id != target_id or parent.status != "DRAFT":
            raise DomainError("The draft is no longer available for generation", 409)
        if parent.revision != expected_revision:
            raise DomainError("Draft changed after generation was requested. Review and try again.", 409)
        if (parent.topic, parent.pillar, parent.format) != (request.topic, request.pillar, request.format):
            raise DomainError("Generation request no longer matches the saved draft", 409)
    request.pillar = canonical_pillar(db, brand_id, request.pillar)
    refs = generation_candidates(db, brand_id, request.topic, request.pillar, request.knowledge_refs)
    if not refs:
        raise DomainError("Approve and enable knowledge for this pillar before AI generation")
    recent = db.scalars(select(Content).where(Content.brand_id == brand_id).order_by(Content.id.desc()).limit(100)).all()
    if not parent and any(similarity(request.topic, c.topic) > .85 for c in recent if c.status != "ARCHIVED"):
        raise DomainError("A similar topic already exists. Repurpose it or choose a different topic.", 409)
    model = provider(config.ai)
    evidence = [{"id": k.id, "title": k.title, "body": k.body[:8000], "source": k.source,
                 "restrictions": k.restrictions,
                 "topic_fit": "adjacent" if editorial.topic_mismatch(request.topic, k.title, k.body) else "direct"}
                for k in refs]
    direct_evidence = [row for row in evidence if row["topic_fit"] == "direct"] or evidence
    direct_ids = {row["id"] for row in direct_evidence}
    plan_refs = [row for row in refs if row.id in direct_ids]
    expected = editorial.headline_count(request.topic)
    plan = model.generate(
        "Plan the post; do not write final copy. State the exact audience promise and select distinct, concrete actions. "
        "Knowledge is evidence, not a required slide outline. Combine compatible records and reject adjacent or repeated ideas. "
        "Do not copy knowledge titles into action headlines; turn source material into specific tasks. "
        "Adjacent evidence titles are listed only as ideas to avoid for this promise. "
        "For numbered headlines select exactly that many actions, excluding the cover. Each point needs supporting knowledge IDs. "
        "Match the promised time frame; a morning activity is not a tonight action. Recommend varied layouts that fit the content. "
        "Keep action headlines under 70 characters and explanations under 180 characters. "
        "Do not expose private reasoning.",
        {"brand_voice": config.editorial.model_dump(), "audience": config.audience, "topic": request.topic,
         "pillar": request.pillar, "format": request.format, "expected_points": expected,
         "knowledge": direct_evidence, "adjacent_titles_to_avoid": [row["title"] for row in evidence if row["topic_fit"] == "adjacent"],
         "parent_hook": parent.hook if parent else ""}, EditorialPlan)
    used_ids = editorial.validate_plan(plan, request.topic, plan_refs)
    used_refs = [k for k in refs if k.id in used_ids]
    plan_data = plan.model_dump()
    base_context = {"brand_voice": config.editorial.model_dump(), "voice": config.voice, "tone": config.tone,
                    "audience": config.audience, "request": request.model_dump(), "plan": plan_data,
                    "knowledge": [row for row in evidence if row["id"] in used_ids],
                    "recent_topics": [c.topic for c in recent[:30]],
                    "parent": {"id": parent.id, "hook": parent.hook, "caption": parent.caption,
                               "slides": parent.slides} if parent else None}
    write_instruction = (
        "Write the final post from the editorial plan. Knowledge is source material, not one slide per record. "
        "For a numbered carousel use one cover plus exactly the planned number of distinct action slides, in plan order. "
        "Every action headline must describe something the audience can do in the promised time frame. "
        "Give each action a short, concrete explanation under 180 characters and optional 2-4 micro-items under 55 characters each. "
        "Keep action headlines under 70 characters and slides phone-readable. "
        "Caption must be 2-3 natural sentences, 90-220 characters: add one specific observation, "
        "refer to at least one actual action from the plan, and end with a practical save/try CTA. "
        "Do not repeat the headline or use generic motivational language. Never put hashtags in caption prose. "
        f"Avoid these exact cliches: {', '.join(config.editorial.banned_phrases)}. "
        "Explain actions with concrete objects and steps. Do not use the words stress, productivity, "
        "sleep quality, self-awareness, or mental load in any slide unless that exact claim appears "
        "in the supplied evidence. Describe the practical next-morning difference instead. "
        "Avoid vague benefit list items. "
        "No engagement bait, invented sources, unsupported factual claims, or advice outside knowledge restrictions. "
        "Use only supplied knowledge IDs in knowledge_refs and put hashtags in the separate hashtags field.")

    def prepare(output):
        output.topic, output.pillar, output.format = request.topic, request.pillar, request.format
        output.parent_id = parent.parent_id if target_id else request.parent_id
        output.targets = parent.targets if parent else config.platforms
        if parent and parent.hook.strip():
            output.hook = parent.hook
        output.knowledge_refs = used_ids
        output.sources = list(dict.fromkeys(k.source for k in used_refs if k.source))
        caption_tags = re.findall(r"(?<!\w)#[A-Za-z][\w]*", output.caption)
        if caption_tags:
            output.caption = re.sub(r"(?<!\w)#[A-Za-z][\w]*", "", output.caption).strip()
            output.hashtags = list(dict.fromkeys(output.hashtags + caption_tags))[:30]
        if config.editorial.hashtag_policy == "none":
            output.hashtags = []
        if len(output.slides) == len(plan.selected_points) + 1:
            output.slides[0].kind = "cover"
            for index, point in enumerate(plan.selected_points, start=1):
                slide = output.slides[index]
                slide.title = point.headline
                # The approved plan remains the source of truth when the prose pass
                # introduces an unsupported benefit or loses the promised time frame.
                if ((editorial.unsupported_claim(slide.body, [k.body for k in used_refs]) or
                     editorial.missing_temporal_link(request.topic, slide.title, slide.body)) and
                    not editorial.unsupported_claim(point.action, [k.body for k in used_refs]) and
                    not editorial.missing_temporal_link(request.topic, point.headline, point.action)):
                    slide.body = point.action
                slide.body = editorial.remove_filler_sentences(slide.body)
                suggested = plan.recommended_layout_sequence[index]
                if suggested in {"cover", "two_column", "do_dont"}:
                    suggested = "numbered_action"
                if suggested in {"checklist", "steps"} and not (point.items or slide.items):
                    suggested = "numbered_action"
                if suggested in {"statement", "tip", "end"} and (point.items or slide.items):
                    suggested = "numbered_action"
                slide.kind = suggested
                if point.items:
                    slide.items = [value for value in point.items if editorial.is_micro_item(value)][:4]
                else:
                    slide.items = [value for value in slide.items if editorial.is_micro_item(value) and len(value) <= 65][:4]
            action_kinds = {slide.kind for slide in output.slides[1:]}
            if len(output.slides) >= 6 and len(action_kinds) == 1:
                middle = output.slides[len(output.slides) // 2]
                middle.kind = "checklist" if middle.items else "statement"
                last = output.slides[-1]
                if not last.items:
                    last.kind = "end"
            if len(output.slides) >= 6:
                for index in range(3, len(output.slides) - 1):
                    if (output.slides[index].kind == output.slides[index - 1].kind ==
                            output.slides[index - 2].kind and not output.slides[index].items):
                        output.slides[index].kind = "statement" if output.slides[index].kind != "statement" else "numbered_action"
                if not output.slides[-1].items:
                    output.slides[-1].kind = "end"
        return output

    output = prepare(model.generate(write_instruction, base_context, GeneratedContent))
    assessment = editorial.evaluate(request.topic, [slide.model_dump() for slide in output.slides],
                                    output.caption, used_ids, config, plan, [k.body for k in used_refs])
    revised = False
    if assessment["needs_revision"]:
        output = prepare(model.generate(
            "Revise this post once. Fix only the listed editorial issues while preserving the plan, evidence, "
            "action count, and requested format. Replace any weak caption completely with 2-3 natural "
            "sentences of 90-220 characters: mention a specific planned action, explain its connection "
            "to the topic promise, and give a practical save/try CTA. Do not use hashtags or cliches "
            "in the caption. Do not introduce claims about stress, productivity, sleep quality, "
            "self-awareness, or mental load unless exactly supported by supplied evidence. "
            "Return the complete corrected structured post.",
            {**base_context, "initial": output.model_dump(), "issues": assessment["issues"]}, GeneratedContent))
        assessment = editorial.evaluate(request.topic, [slide.model_dump() for slide in output.slides],
                                        output.caption, used_ids, config, plan, [k.body for k in used_refs])
        revised = True
    if len(output.slides) != len(plan.selected_points) + 1:
        raise DomainError("Generated carousel does not match the promised action count")
    if assessment["blocking_issues"]:
        raise DomainError("Generated content still needs editorial work after one revision: " +
                          "; ".join(assessment["blocking_issues"][:3]))
    for index, slide in enumerate(output.slides, start=1):
        render_slide(Slide.model_validate(slide.model_dump()), config.visual, index, len(output.slides))
    item = save(db, brand_id, ContentInput.model_validate(output.model_dump()), key=target_id, actor=actor)
    item.generation = {"provider": config.ai.provider, "model": config.ai.model, "knowledge": [
        {"id": k.id, "title": k.title, "source": k.source, "updated_at": k.updated_at.isoformat(),
         "restrictions": k.restrictions} for k in used_refs],
        "editorial_plan": plan_data, "editorial_evaluation": assessment, "ai_revised": revised}
    transition(db, item, "REVIEW", actor)
    return item
