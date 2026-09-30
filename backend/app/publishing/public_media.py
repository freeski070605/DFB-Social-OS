"""Publishing media must be an approved rendered asset, served by a separate provider."""
from typing import Protocol
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from io import BytesIO
from urllib.parse import quote, urlsplit

from app.core.config import settings
from app.core.errors import ProviderError
from app.storage.local import LocalStorage
from app.db.session import utcnow


class PublicMediaProvider(Protocol):
    def prepare(self, content, asset: dict) -> str: ...
    def delete(self, key: str) -> None: ...


def approved_asset(content, asset: dict) -> bytes:
    if content.status not in {"APPROVED", "SCHEDULED", "PUBLISHING"}:
        raise ProviderError("ASSET_APPROVAL_MISMATCH: Approve content before preparing public media")
    if asset not in content.assets or not isinstance(asset, dict):
        raise ProviderError("ASSET_APPROVAL_MISMATCH: Only approved rendered publishing images can be sent to a public media provider")
    key, digest = asset.get("key", ""), asset.get("sha256", "")
    pattern = rf"[a-z0-9_-]+/{content.id}/r{content.revision}-[0-9]+-([0-9a-f]{{24}})\.(png|jpe?g)"
    match = re.fullmatch(pattern, key) if isinstance(key, str) else None
    if not match or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest[:24] != match[1]:
        raise ProviderError("ASSET_APPROVAL_MISMATCH: Only approved rendered publishing images can be sent to a public media provider")
    try:
        data = LocalStorage().read(key)
    except FileNotFoundError:
        raise ProviderError("LOCAL_ASSET_MISSING: Rendered publishing image is missing; render it again before publishing") from None
    except OSError:
        raise ProviderError("ASSET_READ_FAILED: Rendered publishing image could not be read") from None
    if not data:
        raise ProviderError("EMPTY_MEDIA: Rendered publishing image is empty")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
        raise ProviderError("ASSET_APPROVAL_MISMATCH: Rendered publishing image checksum failed")
    return data


class LocalUnavailablePublicMediaProvider:
    def prepare(self, content, asset: dict) -> str:
        approved_asset(content, asset)
        raise ProviderError("Public media storage is unavailable. Configure an external object storage provider before image publishing.")

    def delete(self, key: str) -> None:
        raise ProviderError("Public media storage is unavailable")


PREFIX = "meta-publish/"
KEY_PATTERN = re.compile(r"meta-publish/[0-9a-f]{32}\.(png|jpe?g)")


def r2_ready() -> bool:
    return r2_readiness_issue() is None


def r2_readiness_issue() -> str | None:
    cfg = settings()
    if cfg.public_media_provider != "r2":
        return "R2 public media provider is not selected"
    if not all((cfg.r2_account_id, cfg.r2_access_key_id, cfg.r2_secret_access_key,
                cfg.r2_bucket, cfg.r2_public_base_url)):
        return "R2 configuration is missing required fields"
    if not re.fullmatch(r"[0-9a-f]{32}", cfg.r2_account_id):
        return "R2 account ID format is invalid"
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", cfg.r2_bucket):
        return "R2 bucket name is invalid"
    try:
        url = urlsplit(cfg.r2_public_base_url)
        hostname = url.hostname or ""
    except ValueError:
        return "R2 public base URL is invalid"
    if (url.scheme != "https" or not hostname or "." not in hostname
            or hostname in {"localhost", "127.0.0.1"} or url.username or url.password
            or url.query or url.fragment or url.path not in {"", "/"}
            or hostname.endswith("r2.cloudflarestorage.com")):
        return "R2 public base URL must be a public HTTPS hostname"
    return None


def r2_fingerprint() -> str:
    cfg = settings()
    values = (cfg.r2_account_id, cfg.r2_access_key_id, cfg.r2_secret_access_key,
              cfg.r2_bucket, cfg.r2_public_base_url)
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def r2_verified(db) -> bool:
    if not r2_ready() or db is None:
        return False
    from app.models import SystemSetting
    marker = db.get(SystemSetting, "r2_media_health")
    return bool(marker and marker.value.get("fingerprint") == r2_fingerprint()
                and marker.value.get("checked_at", "") > (utcnow() - timedelta(hours=24)).isoformat())


class R2PublicMediaProvider:
    """Only verified rendered assets enter a dedicated public publishing bucket."""

    def __init__(self, client=None):
        issue = r2_readiness_issue()
        if issue:
            raise ProviderError(issue)
        cfg = settings()
        self.bucket = cfg.r2_bucket
        self.base_url = cfg.r2_public_base_url.rstrip("/")
        if client is None:
            try:
                import boto3
                client = boto3.client("s3", endpoint_url=f"https://{cfg.r2_account_id}.r2.cloudflarestorage.com",
                    aws_access_key_id=cfg.r2_access_key_id, aws_secret_access_key=cfg.r2_secret_access_key,
                    region_name="auto")
            except ImportError:
                raise ProviderError("R2 client dependency is missing") from None
            except Exception:
                raise ProviderError("R2 client could not be initialized") from None
        self.client = client

    def prepare_object(self, content, asset: dict, platform: str | None = None) -> tuple[str, str]:
        from botocore.exceptions import (ClientError, EndpointConnectionError, ConnectTimeoutError,
                                        ReadTimeoutError, SSLError, ParamValidationError)

        data = approved_asset(content, asset)
        extension = asset["key"].rsplit(".", 1)[1]
        if extension == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif extension in {"jpg", "jpeg"} and data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise ProviderError("IMAGE_CONVERSION_FAILED: Rendered asset is not a supported PNG or JPEG image")
        if platform == "instagram" and mime == "image/png":
            from PIL import Image, UnidentifiedImageError
            try:
                with Image.open(BytesIO(data)) as image:
                    output = BytesIO()
                    image.convert("RGB").save(output, format="JPEG", quality=95, optimize=True)
                    data = output.getvalue()
            except (OSError, UnidentifiedImageError, ValueError):
                raise ProviderError("IMAGE_CONVERSION_FAILED: Rendered image cannot be converted to Instagram JPEG") from None
            extension, mime = "jpg", "image/jpeg"
        if not data:
            raise ProviderError("EMPTY_MEDIA: Prepared publishing image is empty")
        if platform == "instagram" and len(data) > 8 * 1024 * 1024:
            raise ProviderError("Instagram image exceeds 8 MB")
        key = PREFIX + secrets.token_hex(16) + "." + extension
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime,
                                   CacheControl="no-store")
        except Exception as exc:
            try:
                self.client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                pass
            if isinstance(exc, SSLError):
                raise ProviderError("R2_ENDPOINT_TLS_FAILED: TLS handshake with the R2 S3 endpoint failed") from None
            if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
                raise ProviderError("R2_PUT_FAILED: R2 S3 endpoint connection failed") from None
            if isinstance(exc, ParamValidationError):
                raise ProviderError("R2_PUT_FAILED: Invalid S3 upload parameters") from None
            if isinstance(exc, ClientError):
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken", "ExpiredToken"}:
                    raise ProviderError("R2 credentials are invalid") from None
                if code in {"NoSuchBucket", "404"}:
                    raise ProviderError("R2 bucket was not found") from None
                if code in {"AccessDenied", "AllAccessDisabled"}:
                    raise ProviderError("R2_WRITE_DENIED: R2 media upload was denied") from None
            raise ProviderError("R2_PUT_FAILED: R2 media upload failed (" + type(exc).__name__ + ")") from None
        return key, self.base_url + "/" + quote(key)

    def prepare(self, content, asset: dict) -> str:
        return self.prepare_object(content, asset)[1]

    def delete(self, key: str) -> None:
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise ProviderError("Invalid temporary media object key")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            raise ProviderError("CLEANUP_FAILED: R2 media cleanup failed", transient=True) from None


def public_media_provider() -> PublicMediaProvider:
    if settings().public_media_provider == "r2":
        return R2PublicMediaProvider()
    return LocalUnavailablePublicMediaProvider()
