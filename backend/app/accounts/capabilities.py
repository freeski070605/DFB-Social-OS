"""Truthful provider capability registry; planned integrations grant no live actions."""
from app.models import PlatformAccount
from app.db.session import utcnow
from app.accounts.credentials import has_credential
from app.publishing.public_media import r2_verified
from sqlalchemy.orm import object_session

CAPABILITIES = ("CAN_PUBLISH_TEXT", "CAN_PUBLISH_IMAGE", "CAN_PUBLISH_CAROUSEL",
                "CAN_PUBLISH_SHORT_VIDEO", "CAN_PUBLISH_LONG_VIDEO", "CAN_FETCH_COMMENTS",
                "CAN_REPLY", "CAN_FETCH_ANALYTICS")
PROVIDERS = {
    "facebook": {"state": "SUPPORTED", "capabilities": ("CAN_PUBLISH_TEXT", "CAN_PUBLISH_IMAGE",
        "CAN_PUBLISH_CAROUSEL", "CAN_FETCH_COMMENTS", "CAN_REPLY", "CAN_FETCH_ANALYTICS")},
    "instagram": {"state": "SUPPORTED", "capabilities": ("CAN_PUBLISH_IMAGE", "CAN_PUBLISH_CAROUSEL",
        "CAN_FETCH_COMMENTS", "CAN_REPLY", "CAN_FETCH_ANALYTICS")},
    "youtube": {"state": "SUPPORTED", "capabilities": ()},
    "tiktok": {"state": "PLANNED", "capabilities": ()},
    "threads": {"state": "PLANNED", "capabilities": ()},
}


def provider_view(platform, accounts: list[PlatformAccount]):
    spec = PROVIDERS[platform]
    active = [a for a in accounts if a.platform == platform and a.enabled and
              has_credential(a) and (a.config or {}).get("token_status") == "healthy" and
              (not (a.config or {}).get("expires_at") or (a.config or {})["expires_at"] > utcnow().timestamp())]
    state = "CONNECTED" if spec["state"] == "SUPPORTED" and len(active) == 1 else (
        "NEEDS_ATTENTION" if spec["state"] == "SUPPORTED" and any(a.platform == platform for a in accounts)
        else "NOT_CONNECTED")
    capabilities = list(spec["capabilities"] if state == "CONNECTED" else ())
    if platform == "facebook" and active:
        scopes = set((active[0].config or {}).get("permissions") or [])
        tasks = (active[0].config or {}).get("tasks") or []
        if "pages_manage_posts" not in scopes or (tasks and "CREATE_CONTENT" not in tasks):
            capabilities = [item for item in capabilities if item not in {"CAN_PUBLISH_TEXT", "CAN_PUBLISH_IMAGE", "CAN_PUBLISH_CAROUSEL"}]
    if platform in {"facebook", "instagram"} and not r2_verified(object_session(active[0]) if active else None):
        capabilities = [item for item in capabilities if item not in {"CAN_PUBLISH_IMAGE", "CAN_PUBLISH_CAROUSEL"}]
    if platform == "instagram" and active:
        scopes = set((active[0].config or {}).get("permissions") or [])
        required = {
            "CAN_PUBLISH_IMAGE": {"pages_show_list", "pages_read_engagement", "instagram_basic", "instagram_content_publish"},
            "CAN_PUBLISH_CAROUSEL": {"pages_show_list", "pages_read_engagement", "instagram_basic", "instagram_content_publish"},
            "CAN_FETCH_COMMENTS": {"instagram_basic", "instagram_manage_comments"},
            "CAN_REPLY": {"instagram_basic", "instagram_manage_comments"},
            "CAN_FETCH_ANALYTICS": {"instagram_basic", "pages_read_engagement"},
        }
        capabilities = [item for item in capabilities if required[item] <= scopes]
    return {"platform": platform, "support": spec["state"], "connection": state,
            "capabilities": capabilities,
            "account_count": sum(a.platform == platform for a in accounts)}
