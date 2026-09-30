"""Private-only manual YouTube publishing with revision-bound, resumable uploads."""
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.accounts.credentials import has_credential
from app.core.config import settings
from app.core.errors import DomainError
from app.db.session import utcnow
from app.models import (Brand, Content, PlatformAccount, Publication, SystemSetting,
                        YoutubeUploadAttempt, YoutubeVideoAsset)
from app.security.secrets import decrypt, encrypt
from app.storage.local import LocalStorage

MAX_VIDEO_BYTES = 256 * 1024**3
CHUNK_SIZE = 8 * 1024 * 1024
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
PRIVATE = "PRIVATE"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
registration_log = logging.getLogger("dfb.video_registration")


class YoutubeProviderError(Exception):
    def __init__(self, message, uncertain=False):
        self.uncertain = uncertain
        super().__init__(message)


def video_metadata(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise DomainError("ffprobe is required to validate YouTube video assets", 503)
    try:
        result = subprocess.run([executable, "-v", "error", "-show_streams", "-show_format",
                                 "-of", "json", str(path)], capture_output=True, text=True,
                                check=True, timeout=60)
        probe = json.loads(result.stdout)
        stream = next(row for row in probe.get("streams", []) if row.get("codec_type") == "video")
        duration = float(stream.get("duration") or probe.get("format", {}).get("duration") or 0)
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        if duration <= 0 or width <= 0 or height <= 0:
            raise ValueError("missing video dimensions or duration")
        return {"duration_seconds": duration, "width": width, "height": height}
    except (OSError, subprocess.SubprocessError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
        raise DomainError("The selected file is not a readable video with duration and dimensions", 422) from None


def _hash_path(path):
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


def store_video_asset(db, item, upload):
    filename = Path(upload.filename or "video").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
        raise DomainError("Choose a supported video file (.mp4, .mov, .m4v, .webm or .mkv)", 422)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")[:200] or "video" + suffix
    key = f"youtube/{item.brand_id}/{item.id}/r{item.revision}/{secrets.token_hex(12)}-{safe_name}"
    storage = LocalStorage()
    destination = storage.path(key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    byte_size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".youtube-", delete=False) as output:
            temp_path = Path(output.name)
            digest = hashlib.sha256()
            while chunk := upload.file.read(1024 * 1024):
                byte_size += len(chunk)
                if byte_size > MAX_VIDEO_BYTES:
                    raise DomainError("Video exceeds YouTube's 256 GB maximum", 413)
                digest.update(chunk)
                output.write(chunk)
        if byte_size == 0:
            raise DomainError("Video file is empty", 422)
        metadata = video_metadata(temp_path)
        os.replace(temp_path, destination)
        record = YoutubeVideoAsset(brand_id=item.brand_id, content_id=item.id, revision=item.revision,
            storage_key=key, filename=safe_name, byte_size=byte_size, sha256=digest.hexdigest(), **metadata)
        db.add(record)
        db.commit()
        db.refresh(record)
        registration_log.info("registration_store_complete content_id=%s revision=%s stored_bytes=%s",
                              item.id, item.revision, byte_size)
        return record
    except Exception as exc:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        registration_log.warning("registration_store_failed content_id=%s revision=%s exception=%s stored_bytes=%s partial_cleaned=true",
                                 item.id, item.revision, type(exc).__name__, byte_size)
        raise


def asset_view(asset):
    return {"id": asset.id, "filename": asset.filename, "byte_size": asset.byte_size,
            "sha256": asset.sha256, "duration_seconds": asset.duration_seconds,
            "width": asset.width, "height": asset.height, "revision": asset.revision}


def verify_asset(item, asset):
    if not asset or asset.content_id != item.id or asset.brand_id != item.brand_id or asset.revision != item.revision:
        raise DomainError("Select a video asset associated with the current approved content revision", 409)
    path = LocalStorage().path(asset.storage_key)
    try:
        byte_size, digest = _hash_path(path)
    except OSError:
        raise DomainError("Approved YouTube video is missing or unreadable", 409) from None
    if byte_size <= 0 or byte_size != asset.byte_size or not hmac.compare_digest(digest, asset.sha256):
        raise DomainError("Approved video changed after selection; select it again and run a new dry run", 409)
    return path


def metadata_idempotency_key(content_id, revision, asset_hash, channel_id):
    raw = f"{content_id}\0{revision}\0{asset_hash}\0{channel_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def blocking_upload_attempt(db, brand_id, content_id):
    attempts = db.scalars(select(YoutubeUploadAttempt).where(
        YoutubeUploadAttempt.brand_id == brand_id, YoutubeUploadAttempt.content_id == content_id)).all()
    return next((attempt for attempt in attempts if attempt.state in {
        "PREPARING", "UPLOADING", "PROCESSING", "RECONCILIATION_REQUIRED", "PUBLISHED"}
        or attempt.provider_video_id or attempt.session_uri or attempt.bytes_sent), None)


def _account(db, brand_id, account_id, allow_refresh=False):
    account = db.get(PlatformAccount, account_id) if account_id else None
    if not account or account.brand_id != brand_id or account.platform != "youtube" or not account.enabled:
        raise DomainError("Select an enabled YouTube channel for this brand", 409)
    config = account.config or {}
    if not has_credential(account) or config.get("token_status") != "healthy" or not config.get("last_checked"):
        raise DomainError("YouTube credential is unhealthy; reconnect and verify the channel", 409)
    if config.get("expires_at", 0) <= utcnow().timestamp() and not (
            allow_refresh and config.get("refresh_token_encrypted")):
        raise DomainError("YouTube credential has expired; reconnect and verify the channel", 409)
    if UPLOAD_SCOPE not in set(config.get("permissions") or []):
        raise DomainError("Additional authorization required for YouTube publishing", 409)
    return account


def validate_metadata(title, description, intended_format, audience):
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise DomainError("YouTube title is required and must be 100 characters or fewer", 422)
    if not isinstance(description, str) or not description.strip() or len(description) > 5000:
        raise DomainError("YouTube description is required and must be 5000 characters or fewer", 422)
    if intended_format not in {"YOUTUBE_SHORT", "YOUTUBE_LONGFORM"}:
        raise DomainError("Select YouTube Short or Long-form", 422)
    if audience not in {"MADE_FOR_KIDS", "NOT_MADE_FOR_KIDS"}:
        raise DomainError("Explicitly select the audience setting", 422)


def dry_run(db, brand_id, content_id, account_id, asset_id, title, description, intended_format, audience):
    item = db.get(Content, content_id)
    if not item or item.brand_id != brand_id:
        raise DomainError("Content not found", 404)
    reasons = []
    brand = db.get(Brand, brand_id)
    if not brand or not brand.enabled or brand.paused:
        reasons.append("Brand outward actions must be active")
    if item.status != "APPROVED":
        reasons.append("Content must be APPROVED at its current revision")
    try:
        validate_metadata(title, description, intended_format, audience)
    except DomainError as exc:
        reasons.append(exc.message)
    account = None
    try:
        account = _account(db, brand_id, account_id)
    except DomainError as exc:
        reasons.append(exc.message)
    asset = db.get(YoutubeVideoAsset, asset_id) if asset_id else None
    if not asset or asset.content_id != item.id or asset.revision != item.revision:
        reasons.append("Select a video associated with the current content revision")
    else:
        try:
            verify_asset(item, asset)
        except DomainError as exc:
            reasons.append(exc.message)
    prior = db.scalar(select(Publication).where(Publication.content_id == content_id,
                                                Publication.platform == "youtube"))
    if prior and prior.state == "PUBLISHED":
        reasons.append("This content already has a successful YouTube receipt")
    if blocking_upload_attempt(db, brand_id, content_id):
        reasons.append("A YouTube upload attempt already exists for this content; reconcile it before another upload")
    if account and asset:
        key = metadata_idempotency_key(content_id, item.revision, asset.sha256, account.account_id)
        existing = db.scalar(select(YoutubeUploadAttempt).where(YoutubeUploadAttempt.idempotency_key == key))
        if existing:
            reasons.append("A YouTube upload attempt already exists for this content, revision, asset and channel")
    if reasons:
        return {"status": "BLOCKED", "reasons": reasons}
    expires_at = (utcnow() + timedelta(minutes=20)).isoformat()
    token = secrets.token_hex(16)
    account_config = account.config or {}
    summary = {"status": "READY", "plan_token": token, "expires_at": expires_at,
        "brand_id": brand_id, "content_id": item.id, "revision": item.revision,
        "account_id": account.id, "channel_id": account.account_id,
        "channel_name": account_config.get("name") or account.account_id,
        "asset_id": asset.id, "storage_key": asset.storage_key, "filename": asset.filename,
        "byte_size": asset.byte_size, "sha256": asset.sha256,
        "duration_seconds": asset.duration_seconds, "width": asset.width, "height": asset.height,
        "title": title, "description": description, "intended_format": intended_format,
        "audience": audience, "privacy": PRIVATE}
    db.add(SystemSetting(key="youtube_manual_plan:" + token, value=summary))
    db.commit()
    return {key: value for key, value in summary.items() if key != "storage_key"}


class YouTubeResumableProvider:
    """Official YouTube Data API v3 resumable upload, using bounded chunks."""
    api = "https://www.googleapis.com/upload/youtube/v3/videos"

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, read=300), trust_env=False,
                                             follow_redirects=False)

    def _response(self, response):
        if response.status_code >= 500:
            raise YoutubeProviderError("YouTube returned a server error during upload", uncertain=True)
        if response.status_code not in {200, 201, 308}:
            raise YoutubeProviderError("YouTube rejected the video upload", uncertain=False)
        return response

    def initiate(self, token, attempt, asset):
        mime = mimetypes.guess_type(asset.filename)[0] or "video/mp4"
        metadata = {"snippet": {"title": attempt.upload_metadata["title"], "description": attempt.upload_metadata["description"]},
                "status": {"privacyStatus": PRIVATE.lower(),
                       "selfDeclaredMadeForKids": attempt.upload_metadata["audience"] == "MADE_FOR_KIDS"}}
        try:
            response = self.client.post(self.api, params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                         "X-Upload-Content-Length": str(asset.byte_size), "X-Upload-Content-Type": mime},
                json=metadata)
            self._response(response)
        except httpx.HTTPError as exc:
            raise YoutubeProviderError("YouTube upload session outcome is ambiguous", uncertain=True) from exc
        session_uri = response.headers.get("Location", "")
        if not session_uri or not session_uri.startswith("https://"):
            raise YoutubeProviderError("YouTube did not return a safe resumable upload session", uncertain=True)
        return session_uri

    def send_chunk(self, token, session_uri, offset, chunk, total):
        end = offset + len(chunk) - 1
        try:
            response = self.client.put(session_uri, content=chunk, headers={
                "Authorization": "Bearer " + token,
                "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total}"})
            self._response(response)
        except httpx.HTTPError as exc:
            raise YoutubeProviderError("YouTube chunk outcome is ambiguous", uncertain=True) from exc
        if response.status_code == 308:
            received = _received_bytes(response.headers.get("Range", ""))
            return received, ""
        try:
            video_id = response.json().get("id", "")
        except ValueError:
            video_id = ""
        if not video_id:
            raise YoutubeProviderError("YouTube confirmed upload bytes without returning a video ID", uncertain=True)
        return total, str(video_id)

    def query_session(self, token, session_uri, total):
        try:
            response = self.client.put(session_uri, content=b"", headers={
                "Authorization": "Bearer " + token, "Content-Length": "0",
                "Content-Range": f"bytes */{total}"})
            self._response(response)
        except httpx.HTTPError as exc:
            raise YoutubeProviderError("YouTube resumable upload status is ambiguous", uncertain=True) from exc
        if response.status_code == 308:
            return _received_bytes(response.headers.get("Range", "")), ""
        try:
            video_id = response.json().get("id", "")
        except ValueError:
            video_id = ""
        if not video_id:
            raise YoutubeProviderError("YouTube upload status did not identify a created video", uncertain=True)
        return total, str(video_id)

    def video_status(self, token, video_id):
        try:
            response = self.client.get("https://www.googleapis.com/youtube/v3/videos",
                params={"part": "processingDetails,status,snippet,suggestions", "id": video_id},
                headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            rows = response.json().get("items", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise YoutubeProviderError("YouTube video status lookup failed") from exc
        if not rows:
            return {"found": False}
        row = rows[0]
        suggestions = row.get("suggestions", {})
        return {"found": True, "video_id": row.get("id"),
                "channel_id": row.get("snippet", {}).get("channelId"),
                "privacy_status": row.get("status", {}).get("privacyStatus"),
                "upload_status": row.get("status", {}).get("uploadStatus"),
                "rejection_reason": row.get("status", {}).get("rejectionReason"),
                "processing_status": row.get("processingDetails", {}).get("processingStatus", "unknown"),
                "processing_failure_reason": row.get("processingDetails", {}).get("processingFailureReason"),
                "processing_issues": {key: suggestions.get(key, []) for key in
                    ("processingErrors", "processingWarnings", "processingHints")}}


def _received_bytes(value):
    match = re.fullmatch(r"bytes=0-(\d+)", value)
    return int(match.group(1)) + 1 if match else 0


def _token(account, refresh=False):
    config = account.config or {}
    if config.get("expires_at", 0) <= utcnow().timestamp() + 30:
        if not refresh or not config.get("refresh_token_encrypted"):
            raise YoutubeProviderError("YouTube access token expired; reconnect or resume after reconnecting")
        cfg = settings()
        try:
            refresh_token = decrypt(config["refresh_token_encrypted"])
            with httpx.Client(timeout=20, trust_env=False) as client:
                response = client.post("https://oauth2.googleapis.com/token", data={
                    "client_id": cfg.youtube_client_id, "client_secret": cfg.youtube_client_secret,
                    "refresh_token": refresh_token, "grant_type": "refresh_token"})
                response.raise_for_status()
                result = response.json()
            token = result.get("access_token")
            if not token:
                raise ValueError("No access token returned")
        except (httpx.HTTPError, ValueError, DomainError):
            raise YoutubeProviderError("YouTube credential refresh failed") from None
        account.token_encrypted = encrypt(token)
        account.config = {**config, "expires_at": int(utcnow().timestamp()) + int(result.get("expires_in", 0)),
                          "token_status": "healthy", "last_checked": utcnow().isoformat() + "Z"}
        session = object_session(account)
        if session:
            session.flush()
        return token
    try:
        return decrypt(account.token_encrypted)
    except DomainError as exc:
        raise YoutubeProviderError(exc.message) from None


def _summary(attempt):
    return {"id": attempt.id, "state": attempt.state, "bytes_sent": attempt.bytes_sent,
            "provider_video_id": attempt.provider_video_id or None, "error": attempt.error,
            "metadata": {key: value for key, value in attempt.upload_metadata.items()
                         if key in {"title", "description", "intended_format", "audience", "privacy", "filename", "asset_sha256", "byte_size"}}}


def _save_receipt(db, attempt):
    existing = db.scalar(select(Publication).where(Publication.content_id == attempt.content_id,
                                                   Publication.platform == "youtube"))
    if existing and existing.external_id and existing.external_id != attempt.provider_video_id:
        attempt.state = "RECONCILIATION_REQUIRED"
        attempt.error = "A different YouTube receipt already exists for this content"
        db.commit()
        return
    if not existing:
        existing = Publication(brand_id=attempt.brand_id, content_id=attempt.content_id,
                              platform="youtube", state="PROCESSING")
        db.add(existing)
    existing.external_id = attempt.provider_video_id
    existing.state = "PROCESSING"
    existing.request_state = {"provider": "youtube", "channel_id": attempt.channel_id,
        "account_id": attempt.account_id, "content_id": attempt.content_id, "revision": attempt.revision,
        "asset_sha256": attempt.asset_sha256, "privacy": PRIVATE,
        "intended_format": attempt.upload_metadata["intended_format"], "audience": attempt.upload_metadata["audience"],
        "timestamp": utcnow().isoformat(), "provider_metadata": {"title": attempt.upload_metadata["title"],
            "description": attempt.upload_metadata["description"], "privacyStatus": PRIVATE.lower(),
            "selfDeclaredMadeForKids": attempt.upload_metadata["audience"] == "MADE_FOR_KIDS"}}
    db.commit()


def _run_attempt(db, attempt, account, resume=False, provider=None):
    provider = provider or YouTubeResumableProvider()
    asset = db.get(YoutubeVideoAsset, attempt.asset_id)
    item = db.get(Content, attempt.content_id)
    if not item or item.status != "APPROVED" or item.revision != attempt.revision:
        attempt.state, attempt.error = "FAILED", "Approved content revision changed before upload"
        db.commit()
        return _summary(attempt)
    try:
        path = verify_asset(item, asset)
    except DomainError as exc:
        attempt.state, attempt.error = "FAILED", exc.message
        db.commit()
        return _summary(attempt)
    try:
        token = _token(account, refresh=True)
        if not attempt.session_uri:
            if resume:
                attempt.state, attempt.error = "RECONCILIATION_REQUIRED", "No saved resumable session is available"
                db.commit()
                return _summary(attempt)
            attempt.state = "PREPARING"
            db.commit()
            attempt.session_uri = provider.initiate(token, attempt, asset)
            attempt.state = "UPLOADING"
            db.commit()
        elif resume:
            attempt.state = "UPLOADING"
            db.commit()
        offset, video_id = provider.query_session(token, attempt.session_uri, asset.byte_size)
        if video_id:
            attempt.bytes_sent, attempt.provider_video_id = asset.byte_size, video_id
        else:
            attempt.bytes_sent = offset
        db.commit()
        if not video_id and offset < asset.byte_size:
            with path.open("rb") as stream:
                stream.seek(offset)
                while chunk := stream.read(CHUNK_SIZE):
                    previous_offset = offset
                    token = _token(account, refresh=True)
                    sent, video_id = provider.send_chunk(token, attempt.session_uri, previous_offset, chunk, asset.byte_size)
                    if sent < previous_offset or sent > previous_offset + len(chunk) or sent > asset.byte_size:
                        raise YoutubeProviderError("YouTube returned an invalid upload offset", uncertain=True)
                    if sent == previous_offset and not video_id:
                        raise YoutubeProviderError("YouTube did not advance the upload offset", uncertain=True)
                    offset = sent
                    attempt.bytes_sent = offset
                    if video_id:
                        attempt.provider_video_id = video_id
                    db.commit()
                    if video_id:
                        break
                    if offset >= asset.byte_size:
                        break
                    if sent < previous_offset + len(chunk):
                        stream.seek(sent)
        if not attempt.provider_video_id:
            raise YoutubeProviderError("YouTube has not confirmed a video ID", uncertain=True)
        attempt.state = "PROCESSING"
        attempt.error = ""
        db.commit()
        _save_receipt(db, attempt)
        return _summary(attempt)
    except YoutubeProviderError as exc:
        attempt.state = "RECONCILIATION_REQUIRED" if exc.uncertain or attempt.bytes_sent > 0 else "FAILED"
        attempt.error = str(exc)
        db.commit()
        return _summary(attempt)
    except Exception:
        attempt.state = "RECONCILIATION_REQUIRED" if attempt.session_uri or attempt.bytes_sent else "FAILED"
        attempt.error = "Upload outcome could not be safely determined"
        db.commit()
        return _summary(attempt)


def confirm(db, brand_id, content_id, payload, actor, provider=None):
    from app.approvals.policy import brand_outward_allowed, outward_lock
    with outward_lock:
        item = db.get(Content, content_id)
        if not item or item.brand_id != brand_id:
            raise DomainError("Content not found", 404)
        if item.status != "APPROVED" or item.revision != payload["revision"]:
            raise DomainError("Content must remain APPROVED and unchanged", 409)
        if payload.get("confirmed") is not True:
            raise DomainError("Explicit owner final confirmation is required", 409)
        plan = db.get(SystemSetting, "youtube_manual_plan:" + payload["plan_token"], populate_existing=True)
        if not plan or plan.value.get("expires_at", "") <= utcnow().isoformat():
            raise DomainError("READY plan expired; run YouTube dry run again", 409)
        if plan.value.get("brand_id") != brand_id or plan.value.get("content_id") != content_id:
            raise DomainError("READY plan belongs to different content; run YouTube dry run again", 409)
        expected_fields = ("revision", "account_id", "asset_id", "title", "description",
                           "intended_format", "audience", "privacy")
        if any(plan.value.get(key) != payload.get(key, PRIVATE if key == "privacy" else None) for key in expected_fields):
            raise DomainError("READY plan or metadata changed; run YouTube dry run again", 409)
        account = _account(db, brand_id, payload["account_id"])
        if account.account_id != plan.value.get("channel_id"):
            raise DomainError("YouTube channel changed after READY; run a new dry run", 409)
        asset = db.get(YoutubeVideoAsset, payload["asset_id"])
        path = verify_asset(item, asset)
        if asset.sha256 != plan.value["sha256"] or asset.byte_size != plan.value["byte_size"]:
            raise DomainError("Video changed after READY; run YouTube dry run again", 409)
        if path.stat().st_size != plan.value["byte_size"]:
            raise DomainError("Video changed after READY; run YouTube dry run again", 409)
        validate_metadata(payload["title"], payload["description"], payload["intended_format"], payload["audience"])
        brand_outward_allowed(db, db.get(Brand, brand_id))
        if blocking_upload_attempt(db, brand_id, content_id):
            raise DomainError("A YouTube attempt already exists; inspect its current state before proceeding", 409)
        key = metadata_idempotency_key(content_id, item.revision, asset.sha256, account.account_id)
        successful = db.scalar(select(Publication).where(Publication.content_id == content_id,
                                                         Publication.platform == "youtube",
                                                         Publication.state == "PUBLISHED"))
        if successful:
            raise DomainError("This content already has a successful YouTube receipt", 409)
        existing = db.scalar(select(YoutubeUploadAttempt).where(YoutubeUploadAttempt.idempotency_key == key))
        if existing:
            raise DomainError("A YouTube attempt already exists; inspect its current state before proceeding", 409)
        attempt = YoutubeUploadAttempt(brand_id=brand_id, content_id=content_id, revision=item.revision,
            asset_id=asset.id, asset_sha256=asset.sha256, account_id=account.id,
            channel_id=account.account_id, idempotency_key=key, state="PREPARING",
            upload_metadata={"title": payload["title"], "description": payload["description"],
                "intended_format": payload["intended_format"], "audience": payload["audience"],
                "privacy": PRIVATE, "filename": asset.filename, "asset_sha256": asset.sha256,
                "byte_size": asset.byte_size})
        db.add(attempt)
        db.flush()
        db.delete(plan)
        from app.audit.service import record
        record(db, "publication.manual_confirmation", item.id, brand_id, actor,
               details={"platform": "youtube", "action_source": "MANUAL_OWNER_CONFIRMED"})
        db.commit()
        return _run_attempt(db, attempt, account, provider=provider)


def resume(db, brand_id, attempt_id, actor, provider=None):
    from app.approvals.policy import brand_outward_allowed, outward_lock
    with outward_lock:
        attempt = db.get(YoutubeUploadAttempt, attempt_id)
        if not attempt or attempt.brand_id != brand_id:
            raise DomainError("YouTube upload attempt not found", 404)
        if attempt.state != "UPLOADING" or not attempt.session_uri:
            raise DomainError("Only an interrupted upload with a saved resumable session can be resumed", 409)
        item = db.get(Content, attempt.content_id)
        if not item or item.status != "APPROVED" or item.revision != attempt.revision:
            raise DomainError("Approved content revision changed; upload cannot be resumed", 409)
        brand_outward_allowed(db, db.get(Brand, brand_id))
        asset = db.get(YoutubeVideoAsset, attempt.asset_id)
        verify_asset(item, asset)
        account = _account(db, brand_id, attempt.account_id, allow_refresh=True)
        return _run_attempt(db, attempt, account, resume=True, provider=provider)


def status(db, brand_id, content_id, provider=None):
    attempt = db.scalar(select(YoutubeUploadAttempt).where(YoutubeUploadAttempt.brand_id == brand_id,
        YoutubeUploadAttempt.content_id == content_id).order_by(YoutubeUploadAttempt.id.desc()))
    if not attempt:
        return None
    if attempt.state == "PROCESSING" and attempt.provider_video_id:
        account = db.get(PlatformAccount, attempt.account_id)
        if not account:
            attempt.state, attempt.error = "RECONCILIATION_REQUIRED", "CREDENTIAL_ACCOUNT_MISSING"
            receipt = db.scalar(select(Publication).where(Publication.content_id == content_id,
                                                           Publication.platform == "youtube"))
            if receipt:
                receipt.state = "RECONCILIATION_REQUIRED"
            db.commit()
            return _summary(attempt)
        provider = provider or YouTubeResumableProvider()
        try:
            video = provider.video_status(_token(account), attempt.provider_video_id)
        except YoutubeProviderError:
            return _summary(attempt)
        receipt = db.scalar(select(Publication).where(Publication.content_id == content_id,
                                                       Publication.platform == "youtube"))
        if not receipt or receipt.external_id != attempt.provider_video_id:
            attempt.state, attempt.error = "RECONCILIATION_REQUIRED", "PROVIDER_RECEIPT_MISMATCH"
            if receipt:
                receipt.state = "RECONCILIATION_REQUIRED"
            db.commit()
            return _summary(attempt)
        changed = False
        if not video.get("found"):
            attempt.state, attempt.error = "RECONCILIATION_REQUIRED", "PROVIDER_VIDEO_NOT_FOUND"
            changed = True
        elif video.get("video_id") != attempt.provider_video_id or video.get("channel_id") != attempt.channel_id or account.account_id != attempt.channel_id:
            attempt.state, attempt.error = "RECONCILIATION_REQUIRED", "PROVIDER_CHANNEL_MISMATCH"
            changed = True
        elif video.get("privacy_status") != "private":
            attempt.state, attempt.error = "RECONCILIATION_REQUIRED", "PROVIDER_PRIVACY_MISMATCH"
            changed = True
        elif video.get("upload_status") == "rejected":
            attempt.state, attempt.error = "FAILED", "PROVIDER_VIDEO_REJECTED"
            changed = True
        elif video.get("processing_status") == "succeeded":
            attempt.state, attempt.error = "PUBLISHED", ""
            changed = True
        elif video.get("processing_status") in {"failed", "terminated"}:
            attempt.state, attempt.error = "FAILED", "PROVIDER_PROCESSING_FAILED"
            changed = True
        if receipt:
            details = {key: video.get(key) for key in ("processing_status", "privacy_status", "upload_status")
                       if video.get(key) is not None}
            for key in ("processing_failure_reason", "rejection_reason"):
                value = video.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
                    details[key] = value
            issues = video.get("processing_issues")
            if isinstance(issues, dict):
                details["processing_issues"] = {key: [item for item in items[:20]
                    if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item)]
                    for key, items in issues.items() if key in {"processingErrors", "processingWarnings", "processingHints"}
                    and isinstance(items, list)}
            updated = {**(receipt.request_state or {}), **details}
            if updated != receipt.request_state or (changed and receipt.state != attempt.state):
                receipt.request_state = updated
                receipt.state = attempt.state
                changed = True
        if changed:
            db.commit()
    return _summary(attempt)
