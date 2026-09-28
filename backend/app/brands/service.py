import json
from sqlalchemy import select
from app.core.config import ROOT
from app.models import Brand, SystemSetting, Template
from app.schemas.domain import BrandInput, BrandConfig
from app.repositories.common import require
from app.audit.service import record


def save_brand(db, data: BrandInput, brand_id=None, actor="admin"):
    obj = require(db, Brand, brand_id) if brand_id else Brand()
    before = {"name": obj.name, "config": obj.config} if brand_id else {}
    for key, value in data.model_dump().items():
        setattr(obj, key, value)
    db.add(obj)
    db.flush()
    record(db, "brand.update" if brand_id else "brand.create", obj.id, obj.id, actor, before, data.model_dump())
    return obj


def seed(db):
    if db.get(SystemSetting, "autopilot") is None:
        db.add(SystemSetting(key="autopilot", value={"paused": True}))
    for path in (ROOT / "brands").glob("*/brand.json"):
        data = BrandInput.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if not db.scalar(select(Brand).where(Brand.slug == data.slug)):
            brand = save_brand(db, data, actor="system")
            for kind in ("checklist", "steps", "two_column", "do_dont", "statement", "tip", "cover", "end"):
                db.add(Template(brand_id=brand.id, name=kind.replace("_", " ").title(), kind=kind, config=BrandConfig.model_validate(brand.config).visual.model_dump()))
