import hashlib
from sqlalchemy import select
from app.models import Brand, Content, Template
from app.schemas.domain import Slide, BrandConfig, Visual
from app.repositories.common import require
from app.creative.renderer import render_slide
from app.content.editorial import headline_count
from app.storage.local import LocalStorage
from app.audit.service import record
from app.core.errors import DomainError


def render_content(db, brand_id, content_id, actor="admin"):
    item = require(db, Content, content_id, brand_id)
    if item.status in {"PUBLISHING", "PUBLISHED", "SCHEDULED"}:
        raise DomainError("Unschedule before rendering. Published assets are immutable.", 409)
    if not item.slides:
        raise DomainError("Add structured slides before rendering")
    brand = require(db, Brand, brand_id)
    visual = BrandConfig.model_validate(brand.config).visual
    templates = {t.kind: t for t in db.scalars(select(Template).where(Template.brand_id == brand_id, Template.enabled.is_(True)))}
    images = []
    for i, data in enumerate(item.slides):
        if not data.get("title", "").strip():
            raise DomainError(f"Slide {i + 1} title is required before rendering")
        slide = Slide.model_validate({**data, "items": [value for value in data.get("items", []) if value.strip()]})
        token_values = visual.model_dump()
        if slide.kind in templates:
            token_values.update({key: value for key, value in templates[slide.kind].config.items()
                                 if key in {"background", "foreground", "accent", "muted"}})
        v = Visual.model_validate(token_values)
        numbered = bool(headline_count(item.topic) and len(item.slides) == headline_count(item.topic) + 1
                        and item.slides[0].get("kind") == "cover")
        images.append(render_slide(slide, v, i + 1, len(item.slides), numbered=numbered))
    storage, assets = LocalStorage(), []
    for i, data in enumerate(images):
        digest = hashlib.sha256(data).hexdigest()[:24]
        key = f"{brand.slug}/{item.id}/r{item.revision}-{i + 1}-{digest}.png"
        storage.write(key, data)
        preview = __import__("PIL.Image", fromlist=["Image"]).open(__import__("io").BytesIO(data))
        preview.thumbnail((324, 405))
        output = __import__("io").BytesIO()
        preview.save(output, "PNG")
        preview_key = key.replace(".png", "-preview.png")
        storage.write(preview_key, output.getvalue())
        assets.append({"key": key, "preview": preview_key, "template": item.slides[i]["kind"], "sha256": hashlib.sha256(data).hexdigest()})
    item.assets = assets
    record(db, "content.render", item.id, brand_id, actor, after={"assets": assets})
    return item
