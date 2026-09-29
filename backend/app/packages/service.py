"""Shared evidence with independently reviewed platform derivatives."""
from sqlalchemy import select

from app.ai.providers import provider
from app.audit.service import record
from app.core.errors import DomainError
from app.models import Brand, Content, ContentDerivative, ContentPackage, Knowledge
from app.packages.schemas import PLATFORMS, VIDEO_FORMATS, PackageInput, VideoPlan, TextPlan
from app.repositories.common import require, serialize
from app.schemas.domain import BrandConfig


def evidence(db, brand_id, ids):
    if not ids or len(ids) != len(set(ids)):
        raise DomainError("Select distinct approved knowledge records")
    rows = [require(db, Knowledge, key, brand_id) for key in ids]
    if any(row.verification != "APPROVED" or not row.enabled for row in rows):
        raise DomainError("Package knowledge must be approved and enabled")
    return rows


def create(db, brand_id, data: PackageInput, actor):
    brand = require(db, Brand, brand_id)
    if data.pillar not in BrandConfig.model_validate(brand.config).pillars:
        raise DomainError("Choose a pillar from this brand")
    rows = evidence(db, brand_id, data.knowledge_refs)
    if any(row.category.casefold() != data.pillar.casefold() for row in rows):
        raise DomainError("Package knowledge must match the pillar")
    item = ContentPackage(brand_id=brand_id, **data.model_dump())
    db.add(item)
    db.flush()
    record(db, "package.create", item.id, brand_id, actor, after={"topic": item.topic})
    return item


def attach_content(db, brand_id, content_id, actor):
    content = require(db, Content, content_id, brand_id)
    if not content.knowledge_refs:
        raise DomainError("Content needs approved grounding before package association")
    existing = db.scalar(select(ContentDerivative).where(ContentDerivative.brand_id == brand_id,
        ContentDerivative.content_id == content_id))
    if existing:
        return require(db, ContentPackage, existing.package_id, brand_id)
    refs = evidence(db, brand_id, content.knowledge_refs)
    item = ContentPackage(brand_id=brand_id, topic=content.topic, pillar=content.pillar,
        audience_promise=content.hook, angle="", knowledge_refs=[row.id for row in refs], status="REVIEW")
    db.add(item)
    db.flush()
    derivative = ContentDerivative(package_id=item.id, brand_id=brand_id, platform="instagram",
        format="INSTAGRAM_CAROUSEL", content_id=content.id, content_revision=content.revision,
        status=content.status, plan={}, output={}, used_knowledge_refs=content.knowledge_refs,
        generation={"source": "existing_content"}, evaluation=content.quality or {})
    db.add(derivative)
    record(db, "package.attach_content", item.id, brand_id, actor,
           after={"content_id": content.id, "revision": content.revision})
    return item


def view(db, package):
    result = serialize(package)
    result["derivatives"] = [serialize(d) for d in db.scalars(select(ContentDerivative).where(
        ContentDerivative.package_id == package.id, ContentDerivative.brand_id == package.brand_id).order_by(ContentDerivative.id))]
    return result


def evaluate_output(output, format, eligible):
    used = set(output.knowledge_refs)
    issues = []
    if not used or not used <= eligible:
        raise DomainError("Derivative cites unapproved or cross-brand knowledge")
    if isinstance(output, VideoPlan):
        if any(not set(scene.knowledge_refs) <= used for scene in output.scenes):
            raise DomainError("Scene cites knowledge outside derivative provenance")
        if any(scene.narration.strip() and not scene.knowledge_refs for scene in output.scenes):
            issues.append("Narrated factual beats need knowledge references")
        if any(not scene.visual_direction.strip() for scene in output.scenes):
            issues.append("Every scene needs a usable visual direction")
        if len({scene.visual_type for scene in output.scenes}) == 1:
            issues.append("Visual plan repeats one treatment throughout")
        if abs(sum(scene.estimated_seconds for scene in output.scenes) - output.target_duration_seconds) > max(15, output.target_duration_seconds * .2):
            issues.append("Scene timing does not match target duration")
        scene_words = sum(len(scene.narration.split()) for scene in output.scenes)
        if len(output.narration.split()) < scene_words * .6:
            issues.append("Full narration omits most scene content")
        if len(output.scenes) < (5 if format == "YOUTUBE_LONGFORM" else 2):
            issues.append("Add more distinct visual beats")
        if format == "YOUTUBE_LONGFORM":
            if len(output.sections) < 3 or not output.examples or not output.chapter_candidates:
                issues.append("Long-form needs sections, examples, and chapter candidates")
            if output.target_duration_seconds < 240:
                issues.append("Long-form plan is too shallow")
        if all(scene.visual_type == "graphic" for scene in output.scenes):
            issues.append("Plan original footage or demonstrations, not graphics alone")
        if len(output.narration.split()) < (100 if format == "YOUTUBE_LONGFORM" else 35):
            issues.append("Narration needs more useful detail")
        unsupported = ("sleep quality", "better night's sleep", "morning stress", "productive start",
                       "feeling overwhelmed", "set yourself up for success", "clear your mind")
        if any(phrase in str(output.model_dump()).casefold() for phrase in unsupported):
            issues.append("Script contains unsupported outcome claims")
    for phrase in ("game changer", "adulting", "you've been doing it wrong", "in today's fast-paced world",
                   "hey there, folks", "like, subscribe", "turn on notifications", "set yourself up for success"):
        if phrase in str(output.model_dump()).casefold():
            issues.append(f"Awkward or banned phrase: {phrase}")
    return {"score": max(0, 95 - 12 * len(issues)), "issues": issues,
            "needs_revision": bool(issues), "note": "Human factual and editorial review required"}


def generate(db, brand_id, package_id, format, actor):
    package = require(db, ContentPackage, package_id, brand_id)
    if db.scalar(select(ContentDerivative).where(ContentDerivative.package_id == package_id,
        ContentDerivative.format == format)):
        raise DomainError("Derivative already exists. Review it before creating another version.", 409)
    brand = require(db, Brand, brand_id)
    config = BrandConfig.model_validate(brand.config)
    refs = evidence(db, brand_id, package.knowledge_refs)
    is_video = format in VIDEO_FORMATS
    schema = VideoPlan if is_video else TextPlan
    rules = {
        "INSTAGRAM_CAROUSEL": "Swipeable, concise, scannable, visual and save-worthy.",
        "FACEBOOK_POST": "Useful standalone post with a native caption and no graphic requirement.",
        "INSTAGRAM_REEL": "Immediate spoken and visual hook, natural vertical-video narration and captions.",
        "TIKTOK_VIDEO": "Immediate hook, conversational pacing, no forced slang or fake trends.",
        "YOUTUBE_SHORT": "Strong first seconds, standalone explanation and useful payoff.",
        "YOUTUBE_LONGFORM": "Original educational video with depth, examples, logical sections and visual demonstrations. Do not stretch a Short or make a slideshow.",
        "THREADS_POST": "Concise conversational text-native insight; no graphic required.",
    }[format]
    context = {"brand": brand.name, "voice": config.editorial.model_dump(), "audience": config.audience,
        "topic": package.topic, "pillar": package.pillar, "audience_promise": package.audience_promise,
        "angle": package.angle, "platform": PLATFORMS[format], "format": format,
        "rules": rules, "knowledge": [{"id": k.id, "title": k.title, "body": k.body,
                                      "source": k.source, "restrictions": k.restrictions} for k in refs]}
    model = provider(config.ai)
    instruction = ("Plan and write one platform-native derivative. Cite only knowledge IDs actually used for factual claims. "
                   "Every factual scene should cite its supporting IDs. Do not force all evidence into the output. "
                   "Use natural language, original visual ideas and concrete examples. " + rules)
    output = model.generate(instruction, context, schema)
    assessment = evaluate_output(output, format, set(package.knowledge_refs))
    revised = False
    if assessment["needs_revision"]:
        output = model.generate("Revise once to fix these issues. Preserve approved facts and cite only used IDs. " + rules,
                                {**context, "draft": output.model_dump(), "issues": assessment["issues"]}, schema)
        assessment = evaluate_output(output, format, set(package.knowledge_refs))
        revised = True
    if any(issue in assessment["issues"] for issue in ("Narrated factual beats need knowledge references",
            "Script contains unsupported outcome claims")):
        raise DomainError("Derivative still has unsupported factual beats after one revision")
    item = ContentDerivative(package_id=package_id, brand_id=brand_id, platform=PLATFORMS[format],
        format=format, status="REVIEW", plan={"rules": rules}, output=output.model_dump(),
        used_knowledge_refs=output.knowledge_refs,
        generation={"provider": config.ai.provider, "model": config.ai.model, "revised": revised},
        evaluation=assessment)
    db.add(item)
    db.flush()
    record(db, "derivative.generate", item.id, brand_id, actor,
           after={"package_id": package_id, "format": format, "knowledge_refs": output.knowledge_refs})
    return item


def production_export(db, brand_id, derivative_id):
    derivative = require(db, ContentDerivative, derivative_id, brand_id)
    if derivative.format not in VIDEO_FORMATS:
        raise DomainError("Production handoff requires a video derivative")
    package = require(db, ContentPackage, derivative.package_id, brand_id)
    brand = require(db, Brand, brand_id)
    refs = evidence(db, brand_id, derivative.used_knowledge_refs)
    plan = VideoPlan.model_validate(derivative.output)
    return {"schema_version": 1, "provider": "manual_export", "brand": {"id": brand.id,
        "name": brand.name, "visual": BrandConfig.model_validate(brand.config).visual.model_dump()},
        "package_id": package.id, "derivative_id": derivative.id, "platform": derivative.platform,
        "format": derivative.format, "aspect_ratio": "16:9" if derivative.format == "YOUTUBE_LONGFORM" else "9:16",
        "script": plan.narration, "scene_plan": [scene.model_dump() for scene in plan.scenes],
        "on_screen_text": [scene.on_screen_text for scene in plan.scenes],
        "visual_directions": [scene.visual_direction for scene in plan.scenes],
        "asset_references": [], "provenance": [{"id": k.id, "title": k.title, "source": k.source}
                                             for k in refs],
        "output_requirements": {"title": plan.title, "description": plan.description,
                                "thumbnail_concept": plan.thumbnail_concept,
                                "target_duration_seconds": plan.target_duration_seconds,
                                "human_review_required": True}}
