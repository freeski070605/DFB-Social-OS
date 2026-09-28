from sqlalchemy import select
from app.core.errors import DomainError


def require(db, model, key, brand_id=None):
    obj = db.get(model, key)
    if obj is None or (brand_id is not None and getattr(obj, "brand_id", None) != brand_id):
        raise DomainError("Record not found", 404)
    return obj


def list_brand(db, model, brand_id, limit=200):
    return db.scalars(select(model).where(model.brand_id == brand_id).order_by(model.id.desc()).limit(limit)).all()


def serialize(obj):
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in {"password_hash", "token_encrypted"}}
