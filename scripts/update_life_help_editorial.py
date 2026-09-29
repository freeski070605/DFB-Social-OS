"""Apply LIFE, APPARENTLY. identity to the existing Life Help brand.

Run from the repository root with .venv/Scripts/python.exe scripts/update_life_help_editorial.py.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from app.audit.service import record  # noqa: E402
from app.brands.service import sync_inherited_templates  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models import Brand  # noqa: E402
from app.schemas.domain import BrandInput  # noqa: E402


def update(db):
    configured = BrandInput.model_validate(json.loads((ROOT / "brands/life_help/brand.json").read_text(encoding="utf-8")))
    brand = db.scalar(select(Brand).where(Brand.slug == "life_help"))
    if brand is None:
        raise RuntimeError("Existing Life Help brand not found")
    current = dict(brand.config or {})
    identity_fields = ("mission", "tagline", "short_bio", "voice", "tone", "audience", "cta_rules", "editorial", "visual")
    desired = configured.config.model_dump()
    next_config = {**current, **{key: desired[key] for key in identity_fields}}
    if brand.name == configured.name and brand.description == configured.description and current == next_config:
        return brand.id, False
    old_visual = BrandInput.model_validate({"name": brand.name, "slug": brand.slug, "config": current}).config.visual.model_dump()
    sync_inherited_templates(db, brand.id, old_visual, desired["visual"],
                             legacy_marks=("DFB / FIELD NOTES",))
    before = {"name": brand.name, "description": brand.description}
    brand.name = configured.name
    brand.description = configured.description
    brand.config = next_config
    record(db, "brand.identity_update", brand.id, brand.id, "system", before=before,
           after={"name": brand.name, "description": brand.description, "mark": desired["visual"]["mark"]})
    return brand.id, True


if __name__ == "__main__":
    with SessionLocal.begin() as db:
        brand_id, changed = update(db)
    print(f"Existing brand {brand_id}: {'LIFE, APPARENTLY. identity applied' if changed else 'already current'}")
