from threading import RLock
from sqlalchemy import select
from app.models import SystemSetting, Approval, Brand
from app.core.errors import DomainError
from app.schemas.domain import BrandConfig
from app.audit.service import record

# Single-process deployment: serializes the final outward gate with pause changes.
outward_lock = RLock()


def outward_allowed(db, brand):
    db.refresh(brand)
    setting = db.get(SystemSetting, "autopilot", populate_existing=True)
    if not brand.enabled or brand.paused or setting is None or setting.value.get("paused", True):
        raise DomainError("Outward actions paused", 409)


def set_pause(db, paused, brand_id=None, actor="admin"):
    with outward_lock:
        if brand_id:
            brand = db.get(Brand, brand_id)
            if not brand:
                raise DomainError("Brand not found", 404)
            brand.paused = paused
        else:
            setting = db.get(SystemSetting, "autopilot")
            setting.value = {"paused": paused}
        record(db, "autopilot.pause" if paused else "autopilot.resume", brand_id or "global", brand_id, actor)
        db.commit()


def permit(db, brand, action, target_id, payload=None, automatic=True):
    if not automatic:
        return True
    mode = BrandConfig.model_validate(brand.config).permissions.get(action, "MANUAL")
    if mode == "AUTO":
        return True
    if mode == "APPROVAL":
        existing = db.scalar(select(Approval).where(Approval.brand_id == brand.id, Approval.action == action,
            Approval.target_id == target_id, Approval.status == "PENDING"))
        if not existing:
            db.add(Approval(brand_id=brand.id, action=action, target_id=target_id, payload=payload or {}))
            record(db, "approval.request", target_id, brand.id, "system", details={"action": action})
    return False
