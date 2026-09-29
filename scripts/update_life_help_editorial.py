"""Apply the temporary Life Help editorial profile to an existing installation.

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
from app.db.session import SessionLocal  # noqa: E402
from app.models import Brand  # noqa: E402
from app.schemas.domain import BrandInput  # noqa: E402


def update(db):
    configured = BrandInput.model_validate(json.loads((ROOT / "brands/life_help/brand.json").read_text(encoding="utf-8")))
    brand = db.scalar(select(Brand).where(Brand.slug == "life_help"))
    if brand is None:
        raise RuntimeError("Existing Life Help brand not found")
    current = dict(brand.config or {})
    desired_editorial = configured.config.editorial.model_dump()
    desired_visual = configured.config.visual.model_dump()
    visual = {**current.get("visual", {}), **desired_visual}
    if current.get("editorial") == desired_editorial and current.get("visual") == visual:
        return brand.id, False
    brand.config = {**current, "editorial": desired_editorial, "visual": visual}
    record(db, "brand.editorial_update", brand.id, brand.id, "system",
           after={"editorial_profile": "temporary_life_help", "visual_mark": visual["mark"]})
    return brand.id, True


if __name__ == "__main__":
    with SessionLocal.begin() as db:
        brand_id, changed = update(db)
    print(f"Life Help brand {brand_id}: {'editorial profile updated' if changed else 'already current'}")
