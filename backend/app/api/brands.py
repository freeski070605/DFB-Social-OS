from fastapi import APIRouter, Depends
from sqlalchemy import select
from app.models import Brand, PlatformAccount, SocialIdentity, Template
from app.security.auth import authenticated
from app.security.secrets import encrypt
from app.db.session import get_db
from app.schemas.domain import BrandInput, AccountInput, TemplateInput
from app.repositories.common import require, serialize, list_brand
from app.brands.service import save_brand
from app.audit.service import record
from app.api.meta_auth import account_view, inspect_account
from app.api.youtube_auth import inspect_account as inspect_youtube
from app.db.session import utcnow
from app.core.errors import DomainError
from app.accounts.capabilities import PROVIDERS, provider_view
from app.accounts.credentials import has_credential
from pydantic import BaseModel, Field
from typing import Literal

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
    return [account_view(a) for a in list_brand(db, PlatformAccount, brand_id)]


@router.get("/{brand_id}/platforms")
def platforms(brand_id: int, db=Depends(get_db)):
    require(db, Brand, brand_id)
    rows = list_brand(db, PlatformAccount, brand_id)
    return [provider_view(platform, rows) for platform in PROVIDERS]


class IdentityInput(BaseModel):
    platform: Literal["facebook", "instagram", "youtube", "tiktok", "threads"]
    display_name: str = Field(default="", max_length=120)
    username: str = Field(default="", max_length=120)


@router.get("/{brand_id}/social-identities")
def identities(brand_id: int, db=Depends(get_db)):
    require(db, Brand, brand_id)
    return [serialize(row) for row in list_brand(db, SocialIdentity, brand_id)]


@router.put("/{brand_id}/social-identities/{platform}")
def identity_save(brand_id: int, platform: str, data: IdentityInput,
                  admin=Depends(authenticated), db=Depends(get_db)):
    require(db, Brand, brand_id)
    if platform != data.platform:
        raise DomainError("Platform mismatch")
    row = db.scalar(select(SocialIdentity).where(SocialIdentity.brand_id == brand_id,
                                                SocialIdentity.platform == platform))
    if not row:
        row = SocialIdentity(brand_id=brand_id, platform=platform)
        db.add(row)
    row.display_name, row.username = data.display_name, data.username
    record(db, "identity.save", platform, brand_id, admin.username,
           after={"display_name": data.display_name, "username": data.username})
    db.commit()
    return serialize(row)


@router.post("/{brand_id}/accounts")
def account_save(brand_id: int, data: AccountInput, admin=Depends(authenticated), db=Depends(get_db)):
    require(db, Brand, brand_id)
    if db.scalar(select(PlatformAccount.id).where(PlatformAccount.account_id == data.account_id,
        PlatformAccount.platform == data.platform, PlatformAccount.brand_id != brand_id)):
        raise DomainError("This Meta account is already assigned to another brand", 409)
    account = db.scalar(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                                                       PlatformAccount.platform == data.platform, PlatformAccount.account_id == data.account_id))
    if not account:
        account = PlatformAccount(brand_id=brand_id, platform=data.platform)
        db.add(account)
    account.account_id, account.token_encrypted, account.enabled = data.account_id, encrypt(data.token), data.enabled
    account.config = {"source": "manual", "token_status": "unchecked", "name": "", "permissions": [], "last_checked": None}
    if data.enabled:
        for other in db.scalars(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                                                              PlatformAccount.platform == data.platform)):
            if other is not account:
                other.enabled = False
    record(db, "account.configure", data.platform, brand_id, admin.username, after={"account_id": data.account_id, "enabled": data.enabled})
    db.commit()
    return account_view(account)


@router.post("/{brand_id}/accounts/{key}/check")
def account_check(brand_id: int, key: int, db=Depends(get_db)):
    account = require(db, PlatformAccount, key, brand_id)
    try:
        if account.platform == "youtube":
            inspect_youtube(account)
            result = account_view(account)
        else:
            result = inspect_account(account)
    except Exception:
        account.config = {**(account.config or {}), "token_status": "unhealthy", "last_checked": utcnow().isoformat() + "Z"}
        db.commit()
        raise
    db.commit()
    return result


@router.post("/{brand_id}/accounts/{key}/activate")
def account_activate(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    account = require(db, PlatformAccount, key, brand_id)
    if (account.config or {}).get("token_status") != "healthy" or not has_credential(account):
        raise DomainError("Verify this account through its provider before selecting it", 409)
    for other in db.scalars(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                                                          PlatformAccount.platform == account.platform)):
        other.enabled = other.id == key
    record(db, "account.activate", key, brand_id, admin.username, after={"platform": account.platform, "account_id": account.account_id})
    db.commit()
    return account_view(account)


@router.delete("/{brand_id}/accounts/{key}")
def disconnect(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    account = require(db, PlatformAccount, key, brand_id)
    if account.platform == "facebook" and any(
        (other.config or {}).get("credential_account_id") == key
        for other in list_brand(db, PlatformAccount, brand_id)
    ):
        raise DomainError("Disconnect the linked Instagram account before removing its Meta Page credential", 409)
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
