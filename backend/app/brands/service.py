import json
from sqlalchemy import select
from app.core.config import ROOT
from app.models import Brand, SystemSetting, Template
from app.schemas.domain import BrandInput, BrandConfig
from app.repositories.common import require
from app.audit.service import record


def sync_inherited_templates(db, brand_id, old_visual, new_visual, legacy_marks=()):
    """Move seeded palettes with their brand while leaving custom template palettes alone."""
    identity_keys = ("background", "foreground", "accent", "muted")
    for template in db.scalars(select(Template).where(Template.brand_id == brand_id)):
        current = dict(template.config or {})
        if all(current.get(key) == old_visual.get(key) for key in identity_keys):
            updated = {**current, **new_visual}
            if current.get("mark") not in {old_visual.get("mark"), *legacy_marks}:
                updated["mark"] = current.get("mark", "")
            template.config = updated


def save_brand(db, data: BrandInput, brand_id=None, actor="admin"):
    obj = require(db, Brand, brand_id) if brand_id else Brand()
    before = {"name": obj.name, "config": obj.config} if brand_id else {}
    old_visual = BrandConfig.model_validate(obj.config).visual.model_dump() if brand_id else None
    for key, value in data.model_dump().items():
        setattr(obj, key, value)
    db.add(obj)
    db.flush()
    if old_visual:
        sync_inherited_templates(db, obj.id, old_visual, data.config.visual.model_dump())
    record(db, "brand.update" if brand_id else "brand.create", obj.id, obj.id, actor, before, data.model_dump())
    return obj


def seed(db):
    if db.get(SystemSetting, "autopilot") is None:
        db.add(SystemSetting(key="autopilot", value={"paused": True}))
    for path in (ROOT / "brands").glob("*/brand.json"):
        data = BrandInput.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if not db.scalar(select(Brand).where(Brand.slug == data.slug)):
            brand = save_brand(db, data, actor="system")
            for kind in ("checklist", "steps", "two_column", "do_dont", "statement", "tip", "cover", "end", "numbered_action"):
                db.add(Template(brand_id=brand.id, name=kind.replace("_", " ").title(), kind=kind, config=BrandConfig.model_validate(brand.config).visual.model_dump()))
