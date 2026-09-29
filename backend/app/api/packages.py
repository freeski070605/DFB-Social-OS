from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.db.session import get_db
from app.models import ContentPackage, ContentDerivative
from app.packages import service
from app.packages.schemas import PackageInput, GenerateInput
from app.repositories.common import require, serialize
from app.security.auth import authenticated

router = APIRouter(prefix="/api/brands/{brand_id}/packages", tags=["packages"],
                   dependencies=[Depends(authenticated)])


@router.get("")
def packages(brand_id: int, db=Depends(get_db)):
    return [service.view(db, p) for p in db.scalars(select(ContentPackage).where(
        ContentPackage.brand_id == brand_id).order_by(ContentPackage.id.desc()).limit(100))]


@router.post("")
def create(brand_id: int, data: PackageInput, admin=Depends(authenticated), db=Depends(get_db)):
    item = service.create(db, brand_id, data, admin.username)
    db.commit()
    return service.view(db, item)


@router.post("/from-content/{content_id}")
def from_content(brand_id: int, content_id: int, admin=Depends(authenticated), db=Depends(get_db)):
    item = service.attach_content(db, brand_id, content_id, admin.username)
    db.commit()
    return service.view(db, item)


@router.get("/{package_id}")
def detail(brand_id: int, package_id: int, db=Depends(get_db)):
    return service.view(db, require(db, ContentPackage, package_id, brand_id))


@router.post("/{package_id}/derivatives")
def generate(brand_id: int, package_id: int, data: GenerateInput, admin=Depends(authenticated), db=Depends(get_db)):
    item = service.generate(db, brand_id, package_id, data.format, admin.username)
    db.commit()
    return serialize(item)


@router.get("/{package_id}/derivatives/{derivative_id}/production")
def production(brand_id: int, package_id: int, derivative_id: int, db=Depends(get_db)):
    item = require(db, ContentDerivative, derivative_id, brand_id)
    if item.package_id != package_id:
        from app.core.errors import DomainError
        raise DomainError("Record not found", 404)
    return service.production_export(db, brand_id, derivative_id)
