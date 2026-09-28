"""Apply the configured Life Help pillars to an existing installation.

Run with .venv/Scripts/python.exe scripts/update_life_help_pillars.py.
"""

import csv
import io
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.audit.service import record  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.knowledge import bulk  # noqa: E402
from app.models import Brand  # noqa: E402
from app.schemas.domain import BrandInput  # noqa: E402


def update(db):
    configured = BrandInput.model_validate(json.loads((ROOT / "brands/life_help/brand.json").read_text(encoding="utf-8")))
    if configured.slug != "life_help":
        raise RuntimeError("Life Help configuration has the wrong slug")
    pillars = configured.config.pillars
    if len(pillars) != 14 or len({bulk.normalized(name) for name in pillars}) != 14:
        raise RuntimeError("Life Help must have 14 distinct configured pillars")
    brand = db.scalar(select(Brand).where(Brand.slug == "life_help"))
    if brand is None:
        raise RuntimeError("Existing Life Help brand was not found; no brand was created")
    current = list((brand.config or {}).get("pillars", []))
    if current == pillars:
        return brand.id, False
    brand.config = {**(brand.config or {}), "pillars": pillars}
    record(db, "brand.update", brand.id, brand.id, "system",
           before={"pillars": current}, after={"pillars": pillars})
    return brand.id, True


def validate(db, brand_id):
    pillars = db.get(Brand, brand_id).config["pillars"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=bulk.COLUMNS)
    writer.writeheader()
    nonce = secrets.token_hex(8)
    for number, pillar in enumerate(pillars, start=1):
        writer.writerow({"title": f"Pillar validation {nonce} {number}", "category": pillar,
                         "body": f"Representative validation text {nonce} {number}", "source": "validation sample",
                         "usage_restrictions": "", "tags": "", "verification": "PENDING",
                         "available_for_retrieval": "false"})
    result = bulk.plan(db, brand_id, output.getvalue(), "csv")
    accepted = [row["record"]["category"] for row in result["rows"] if row["status"] == "VALID"]
    if result["total"] != 14 or result["valid"] != 14 or accepted != pillars:
        raise RuntimeError(f"Life Help pillar import validation failed: {result['valid']}/14 accepted")
    return result["valid"]


if __name__ == "__main__":
    with SessionLocal.begin() as db:
        brand_id, changed = update(db)
        accepted = validate(db, brand_id)
    print(f"Life Help brand {brand_id}: {'updated' if changed else 'already current'}; "
          f"Knowledge import categories accepted: {accepted}/14")
