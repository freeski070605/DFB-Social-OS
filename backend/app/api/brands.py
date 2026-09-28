from fastapi import APIRouter, Depends
from sqlalchemy import select
from app.models import Brand, PlatformAccount, Template
from app.security.auth import authenticated
from app.security.secrets import encrypt
from app.db.session import get_db
from app.schemas.domain import BrandInput, AccountInput, TemplateInput
from app.repositories.common import require, serialize, list_brand
from app.brands.service import save_brand
from app.audit.service import record
from app.publishing.providers import MetaPublisher

router = APIRouter(prefix="/api/brands", tags=["brands"], dependencies=[Depends(authenticated)])


@router.get("")
def brands(db=Depends(get_db)):
    return [serialize(b) for b in db.scalars(select(Brand).order_by(Brand.id))]


@router.post("")
def create(data: BrandInput, admin=Depends(authenticated), db=Depends(get_db)):
    result = save_brand(db, data, actor=admin.username)
    db.commit()
    return serialize(result)


@router.put("/{brand_id}")
def update(brand_id: int, data: BrandInput, admin=Depends(authenticated), db=Depends(get_db)):
    result = save_brand(db, data, brand_id, admin.username)
    db.commit()
    return serialize(result)


@router.get("/{brand_id}/accounts")
def accounts(brand_id: int, db=Depends(get_db)):
    return [{**serialize(a), "token_configured": bool(a.token_encrypted)} for a in list_brand(db, PlatformAccount, brand_id)]


@router.post("/{brand_id}/accounts")
def account_save(brand_id: int, data: AccountInput, admin=Depends(authenticated), db=Depends(get_db)):
    require(db, Brand, brand_id)
    account = db.scalar(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id, PlatformAccount.platform == data.platform))
    if not account:
        account = PlatformAccount(brand_id=brand_id, platform=data.platform)
        db.add(account)
    account.account_id, account.token_encrypted, account.enabled = data.account_id, encrypt(data.token), data.enabled
    record(db, "account.configure", data.platform, brand_id, admin.username, after={"account_id": data.account_id, "enabled": data.enabled})
    db.commit()
    return {**serialize(account), "token_configured": True}


@router.post("/{brand_id}/accounts/{key}/check")
def account_check(brand_id: int, key: int, db=Depends(get_db)):
    account = require(db, PlatformAccount, key, brand_id)
    return MetaPublisher().health(account)


@router.delete("/{brand_id}/accounts/{key}")
def disconnect(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    account = require(db, PlatformAccount, key, brand_id)
    db.delete(account)
    record(db, "account.disconnect", key, brand_id, admin.username)
    db.commit()
    return {"ok": True}


@router.get("/{brand_id}/templates")
def templates(brand_id: int, db=Depends(get_db)):
    return [serialize(t) for t in list_brand(db, Template, brand_id)]


@router.post("/{brand_id}/templates")
@router.put("/{brand_id}/templates/{key}")
def template_save(brand_id: int, data: TemplateInput, key: int | None = None, admin=Depends(authenticated), db=Depends(get_db)):
    require(db, Brand, brand_id)
    item = require(db, Template, key, brand_id) if key else Template(brand_id=brand_id)
    for field, value in data.model_dump().items():
        setattr(item, field, value)
    db.add(item)
    record(db, "template.save", key or "new", brand_id, admin.username, after=data.model_dump())
    db.commit()
    return serialize(item)
