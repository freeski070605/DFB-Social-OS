"""Publishing media must be an approved rendered asset, served by a separate provider."""
from typing import Protocol
import hashlib
import hmac
import re

from app.core.errors import ProviderError
from app.storage.local import LocalStorage


class PublicMediaProvider(Protocol):
    def prepare(self, content, asset: dict) -> str: ...


def approved_asset(content, asset: dict) -> bytes:
    if content.status not in {"APPROVED", "SCHEDULED", "PUBLISHING"}:
        raise ProviderError("Approve content before preparing public media")
    if asset not in content.assets or not isinstance(asset, dict):
        raise ProviderError("Only approved rendered publishing images can be sent to a public media provider")
    key, digest = asset.get("key", ""), asset.get("sha256", "")
    pattern = rf"[a-z0-9_-]+/{content.id}/r{content.revision}-[0-9]+-([0-9a-f]{{24}})\.png"
    match = re.fullmatch(pattern, key) if isinstance(key, str) else None
    if not match or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest[:24] != match[1]:
        raise ProviderError("Only approved rendered publishing images can be sent to a public media provider")
    try:
        data = LocalStorage().read(key)
    except OSError:
        raise ProviderError("Rendered publishing image is missing; render it again before publishing")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
        raise ProviderError("Rendered publishing image checksum failed")
    return data


class LocalUnavailablePublicMediaProvider:
    def prepare(self, content, asset: dict) -> str:
        approved_asset(content, asset)
        raise ProviderError("Public media storage is unavailable. Configure an external object storage provider before image publishing.")


def public_media_provider() -> PublicMediaProvider:
    # S3 settings are reserved for a provider adapter; they never make local files public.
    return LocalUnavailablePublicMediaProvider()
