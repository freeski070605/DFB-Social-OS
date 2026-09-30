"""Truthful provider capability registry; planned integrations grant no live actions."""
from app.models import PlatformAccount
from app.db.session import utcnow
from app.accounts.credentials import has_credential

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
    if platform == "instagram" and active:
        scopes = set((active[0].config or {}).get("permissions") or [])
        required = {
            "CAN_PUBLISH_IMAGE": {"instagram_basic", "instagram_content_publish"},
            "CAN_PUBLISH_CAROUSEL": {"instagram_basic", "instagram_content_publish"},
            "CAN_FETCH_COMMENTS": {"instagram_basic", "instagram_manage_comments"},
            "CAN_REPLY": {"instagram_basic", "instagram_manage_comments"},
            "CAN_FETCH_ANALYTICS": {"instagram_basic", "pages_read_engagement"},
        }
        capabilities = [item for item in capabilities if required[item] <= scopes]
    return {"platform": platform, "support": spec["state"], "connection": state,
            "capabilities": capabilities,
            "available_capabilities": list(spec["capabilities"]), "account_count": sum(a.platform == platform for a in accounts)}
