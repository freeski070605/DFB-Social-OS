from pathlib import Path
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func, text
from app.security.auth import authenticated
from app.db.session import get_db
from app.models import Brand, Content, Job, Approval, Interaction, Audit, SystemSetting
from app.repositories.common import require, serialize
from app.schemas.domain import BrandConfig
from app.ai.providers import provider
from app.approvals.policy import set_pause
from app.core.config import ROOT, settings
from app.publishing.public_media import r2_ready, r2_verified
from app.storage.local import LocalStorage
from app.services.backup import backup, restore
from app.audit.service import record

router = APIRouter(prefix="/api", tags=["system"], dependencies=[Depends(authenticated)])


@router.get("/brands/{brand_id}/overview")
def overview(brand_id: int, db=Depends(get_db)):
    brand = require(db, Brand, brand_id)
    counts = dict(db.execute(select(Content.status, func.count()).where(Content.brand_id == brand_id).group_by(Content.status)).all())
    queue = db.scalar(select(func.count()).select_from(Job).where(Job.brand_id == brand_id, Job.status.in_(["PENDING", "WAITING", "PAUSED"])))
    approvals = db.scalar(select(func.count()).select_from(Approval).where(Approval.brand_id == brand_id, Approval.status == "PENDING"))
    inbox = db.scalar(select(func.count()).select_from(Interaction).where(Interaction.brand_id == brand_id, Interaction.status.in_(["NEW", "REVIEW", "UNKNOWN"])))
    config = BrandConfig.model_validate(brand.config)
    daily = max(1 / 7, len(config.schedule.weekdays) * len(config.schedule.times) / 7)
    ready = counts.get("APPROVED", 0) + counts.get("SCHEDULED", 0)
    activity = db.scalars(select(Audit).where((Audit.brand_id == brand_id) | Audit.brand_id.is_(None)).order_by(Audit.id.desc()).limit(8)).all()
    schedule = db.scalars(select(Content).where(Content.brand_id == brand_id, Content.status == "SCHEDULED").order_by(Content.scheduled_at).limit(10)).all()
    return {"brand": serialize(brand), "paused": db.get(SystemSetting, "autopilot").value["paused"], "counts": counts,
            "queue": queue, "approvals": approvals, "community": inbox, "coverage": round(ready / daily, 1),
            "activity": [serialize(a) for a in activity], "schedule": [serialize(c) for c in schedule]}


class PauseInput(BaseModel):
    paused: bool
    brand_id: int | None = None


@router.post("/pause")
def pause(data: PauseInput, admin=Depends(authenticated), db=Depends(get_db)):
    set_pause(db, data.paused, data.brand_id, admin.username)
    return {"paused": data.paused}


@router.get("/system")
def system(brand_id: int, db=Depends(get_db)):
    brand = require(db, Brand, brand_id)
    db.execute(text("SELECT 1"))
    storage = LocalStorage()
    import shutil
    disk = shutil.disk_usage(storage.root)
    from app.creative.renderer import FONT
    global_pause = db.get(SystemSetting, "autopilot")
    return {"database": "ready", "storage": "ready", "free_gb": round(disk.free / 1024**3, 1),
        "renderer": "ready" if FONT.exists() else "font_missing", "ai": provider(BrandConfig.model_validate(brand.config).ai).health(),
        "scheduler": "enabled" if settings().scheduler_enabled else "disabled", "encryption": "configured" if settings().encryption_key else "missing",
        "public_media": "verified" if r2_verified(db) else "configured" if r2_ready() else "unavailable", "meta_api_version": settings().meta_api_version,
        "webhooks": "configured" if settings().meta_app_secret and settings().meta_verify_token else "missing",
        "global_paused": global_pause.value.get("paused", True) if global_pause else True,
        "brand_paused": brand.paused, "brand_enabled": brand.enabled}


@router.post("/backup")
def create_backup(admin=Depends(authenticated), db=Depends(get_db)):
    path = backup()
    record(db, "system.backup", path.name, actor=admin.username)
    db.commit()
    return {"name": path.name, "path": str(path), "note": "Preserve the encryption key separately; encrypted account credentials and generated media are included"}


@router.get("/backups")
def list_backups():
    folder = ROOT / "data" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    return [{"name": path.name, "size": path.stat().st_size, "created_at": path.stat().st_mtime}
            for path in sorted(folder.glob("dfb-*.zip"), key=lambda item: item.stat().st_mtime, reverse=True)]


def backup_file(name: str):
    if Path(name).name != name or not name.startswith("dfb-") or not name.endswith(".zip"):
        from app.core.errors import DomainError
        raise DomainError("Invalid backup name")
    folder = ROOT / "data" / "backups"
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        from app.core.errors import DomainError
        raise DomainError("Backup not found", 404)
    return path


@router.get("/backups/{name}")
def download_backup(name: str):
    path = backup_file(name)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.post("/backups/{name}/restore")
def restore_backup(name: str, admin=Depends(authenticated), db=Depends(get_db)):
    path = backup_file(name)
    safety_copy = backup()
    username = admin.username
    db.close()
    restored = restore(path)
    from app.db.session import SessionLocal
    with SessionLocal() as restored_db:
        record(restored_db, "system.restore", path.name, actor=username, details={"safety_backup": safety_copy.name})
        restored_db.commit()
    return {"restored": path.name, "safety_backup": safety_copy.name, "database": str(restored),
            "note": "Restored sessions were cleared and autopilot remains paused. Sign in again."}


@router.get("/activity")
def global_activity(db=Depends(get_db)):
    return [serialize(a) for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(500))]
