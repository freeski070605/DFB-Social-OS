"""Owner-confirmed, private-only YouTube publishing endpoints."""
from typing import Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.errors import DomainError
from app.db.session import get_db
from app.models import Content, YoutubeUploadAttempt, YoutubeVideoAsset
from app.repositories.common import require
from app.security.auth import authenticated
from app.publishing.youtube import (asset_view, confirm, dry_run, resume, status,
                                    store_video_asset)

router = APIRouter(prefix="/api/brands/{brand_id}/content/{key}", tags=["youtube-publishing"],
                   dependencies=[Depends(authenticated)])


@router.get("/youtube-assets")
def youtube_assets(brand_id: int, key: int, db=Depends(get_db)):
    item = require(db, Content, key, brand_id)
    rows = db.scalars(select(YoutubeVideoAsset).where(YoutubeVideoAsset.content_id == item.id,
        YoutubeVideoAsset.revision == item.revision).order_by(YoutubeVideoAsset.id.desc())).all()
    return [asset_view(row) for row in rows]


@router.post("/youtube-assets")
def youtube_asset_upload(brand_id: int, key: int, file: UploadFile = File(...),
                         admin=Depends(authenticated), db=Depends(get_db)):
    item = require(db, Content, key, brand_id)
    if item.status != "APPROVED":
        raise DomainError("Approve the current content revision before selecting its YouTube video", 409)
    return asset_view(store_video_asset(db, item, file))


class YouTubeDryRunInput(BaseModel):
    account_id: int
    asset_id: int
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=5000)
    intended_format: Literal["YOUTUBE_SHORT", "YOUTUBE_LONGFORM"] | None = None
    audience: Literal["MADE_FOR_KIDS", "NOT_MADE_FOR_KIDS"] | None = None


@router.post("/youtube-dry-run")
def youtube_dry_run(brand_id: int, key: int, data: YouTubeDryRunInput,
                    admin=Depends(authenticated), db=Depends(get_db)):
    result = dry_run(db, brand_id, key, data.account_id, data.asset_id, data.title,
                     data.description, data.intended_format, data.audience)
    return result


class YouTubeConfirmationInput(YouTubeDryRunInput):
    revision: int
    plan_token: str = Field(min_length=32, max_length=32)
    confirmed: bool
    privacy: Literal["PRIVATE"] = "PRIVATE"


@router.post("/youtube-confirm")
def youtube_confirm(brand_id: int, key: int, data: YouTubeConfirmationInput,
                    admin=Depends(authenticated), db=Depends(get_db)):
    result = confirm(db, brand_id, key, data.model_dump(), admin.username)
    return result


@router.get("/youtube-upload")
def youtube_upload_status(brand_id: int, key: int, db=Depends(get_db)):
    return status(db, brand_id, key)


@router.post("/youtube-upload/{attempt_id}/resume")
def youtube_upload_resume(brand_id: int, key: int, attempt_id: int,
                          admin=Depends(authenticated), db=Depends(get_db)):
    attempt = db.get(YoutubeUploadAttempt, attempt_id)
    if not attempt or attempt.content_id != key or attempt.brand_id != brand_id:
        raise DomainError("YouTube upload attempt not found", 404)
    return resume(db, brand_id, attempt_id, admin.username)