"""Facebook Login connection for Pages and Page-linked Instagram professional accounts."""
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.audit.service import record
from app.core.config import settings
from app.core.errors import DomainError, ProviderError
from app.db.session import get_db, utcnow
from app.models import Brand, PlatformAccount, SystemSetting
from app.repositories.common import require
from app.security.auth import authenticated
from app.security.secrets import encrypt, decrypt

router = APIRouter(prefix="/api", tags=["meta"])
SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "instagram_basic",
          "instagram_content_publish", "pages_manage_engagement", "instagram_manage_comments")


def configured():
    cfg = settings()
    if not cfg.meta_app_id or not cfg.meta_app_secret or not cfg.meta_redirect_uri.startswith("https://"):
        raise DomainError("Set DFB_META_APP_ID, DFB_META_APP_SECRET, and an HTTPS DFB_META_REDIRECT_URI in .env")
    if not cfg.encryption_key:
        raise DomainError("Set DFB_ENCRYPTION_KEY before connecting Meta")
    return cfg


def graph(path, *, token=None, params=None):
    cfg = settings()
    url = f"https://graph.facebook.com/{cfg.meta_api_version}/{path}"
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.get(url, params=params, headers={"Authorization": f"Bearer {token}"} if token else {})
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise ProviderError("Meta authorization request failed. Retry connection or check Meta service status.")
    if response.is_error or not isinstance(result, dict) or "error" in result:
        raise ProviderError("Meta rejected authorization or account discovery. Review app permissions and reconnect.")
    return result


def token_metadata(token):
    cfg = settings()
    if not cfg.meta_app_id or not cfg.meta_app_secret:
        raise DomainError("Set DFB_META_APP_ID and DFB_META_APP_SECRET to inspect Meta tokens")
    result = graph("debug_token", params={"input_token": token, "access_token": f"{cfg.meta_app_id}|{cfg.meta_app_secret}"})["data"]
    return {"valid": bool(result.get("is_valid")), "expires_at": result.get("expires_at") or None,
            "data_access_expires_at": result.get("data_access_expires_at") or None,
            "scopes": result.get("scopes", []), "token_type": result.get("type", "")}


def account_view(account):
    config = account.config or {}
    return {"id": account.id, "platform": account.platform, "account_id": account.account_id,
            "enabled": account.enabled, "token_configured": bool(account.token_encrypted),
            "name": config.get("name", ""), "source": config.get("source", "manual"),
            "permissions": config.get("permissions", []), "tasks": config.get("tasks", []),
            "token_status": config.get("token_status", "unchecked"),
            "expires_at": config.get("expires_at"), "data_access_expires_at": config.get("data_access_expires_at"),
            "token_type": config.get("token_type", ""),
            "last_checked": config.get("last_checked")}


def inspect_account(account):
    token = decrypt(account.token_encrypted)
    meta = token_metadata(token) if settings().meta_app_id and settings().meta_app_secret else {}
    identity = graph(account.account_id, token=token, params={"fields": "id,name" if account.platform == "facebook" else "id,username"})
    if str(identity.get("id")) != account.account_id:
        raise ProviderError("Meta returned a different account ID. Reconnect this account.")
    old = account.config or {}
    expires = meta.get("expires_at")
    data_expires = meta.get("data_access_expires_at")
    status = "healthy" if meta.get("valid", True) and (not expires or expires > utcnow().timestamp()) and (not data_expires or data_expires > utcnow().timestamp()) else "expired"
    account.config = {**old, "name": identity.get("name") or identity.get("username") or old.get("name", ""),
                      "token_status": status, "expires_at": expires, "data_access_expires_at": meta.get("data_access_expires_at"),
                      "permissions": meta.get("scopes", old.get("permissions", [])),
                      "token_type": meta.get("token_type", old.get("token_type", "")),
                      "last_checked": utcnow().isoformat() + "Z"}
    return account_view(account)


@router.post("/brands/{brand_id}/meta/connect")
def start_connection(brand_id: int, request: Request, admin=Depends(authenticated), db=Depends(get_db)):
    cfg = configured()
    require(db, Brand, brand_id)
    state = secrets.token_urlsafe(32)
    key = "meta_oauth:" + hashlib.sha256(state.encode()).hexdigest()
    db.add(SystemSetting(key=key, value={"brand_id": brand_id, "admin_id": admin.id,
                                         "session_hash": request.state.session.token_hash,
                                         "expires_at": (utcnow() + timedelta(minutes=10)).isoformat()}))
    record(db, "meta.connect_start", brand_id, brand_id, admin.username)
    db.commit()
    params = {"client_id": cfg.meta_app_id, "redirect_uri": cfg.meta_redirect_uri, "state": state,
              "response_type": "code", "scope": ",".join(SCOPES)}
    return {"authorization_url": "https://www.facebook.com/" + cfg.meta_api_version + "/dialog/oauth?" + urlencode(params)}


@router.get("/meta/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "", admin=Depends(authenticated), db=Depends(get_db)):
    cfg = configured()
    key = "meta_oauth:" + hashlib.sha256(state.encode()).hexdigest()
    pending = db.get(SystemSetting, key) if state else None
    if not pending or pending.value.get("admin_id") != admin.id or pending.value.get("session_hash") != request.state.session.token_hash or pending.value.get("expires_at", "") < utcnow().isoformat():
        raise DomainError("Meta connection expired or state validation failed. Start again from Settings.", 403)
    brand_id = pending.value["brand_id"]
    db.delete(pending)
    db.commit()  # The state is one use, including when Meta returns an error.
    if error or not code:
        raise DomainError("Meta authorization was cancelled or denied. Start connection again.", 400)
    short = graph("oauth/access_token", params={"client_id": cfg.meta_app_id, "client_secret": cfg.meta_app_secret,
                                                   "redirect_uri": cfg.meta_redirect_uri, "code": code})["access_token"]
    exchanged = graph("oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": cfg.meta_app_id,
                                                       "client_secret": cfg.meta_app_secret, "fb_exchange_token": short})
    user_token = exchanged["access_token"]
    permissions = graph("me/permissions", token=user_token).get("data", [])
    granted = sorted(p["permission"] for p in permissions if p.get("status") == "granted" and p.get("permission"))
    for existing in db.scalars(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                                                              PlatformAccount.platform.in_(["facebook", "instagram"]))):
        existing.enabled = False
    after = None
    connected = 0
    selected = set()
    for _ in range(10):
        params = {"fields": "id,name,access_token,tasks,instagram_business_account", "limit": 100}
        if after:
            params["after"] = after
        page = graph("me/accounts", token=user_token, params=params)
        for found in page.get("data", []):
            page_id, page_token = str(found.get("id", "")), found.get("access_token", "")
            if not page_id.isdigit() or not page_token:
                continue
            meta = token_metadata(page_token)
            if not meta["valid"]:
                continue
            common = {"source": "oauth", "page_id": page_id, "permissions": granted,
                      "tasks": found.get("tasks", []), "expires_at": meta["expires_at"],
                      "data_access_expires_at": meta["data_access_expires_at"], "token_status": "healthy",
                      "token_type": meta.get("token_type", ""),
                      "last_checked": utcnow().isoformat() + "Z"}
            for platform, identity, name in (("facebook", page_id, found.get("name", "")),
                                             ("instagram", str(found.get("instagram_business_account", {}).get("id", "")), "")):
                if not identity or not identity.isdigit():
                    continue
                if db.scalar(select(PlatformAccount.id).where(PlatformAccount.platform == platform,
                    PlatformAccount.account_id == identity, PlatformAccount.brand_id != brand_id)):
                    continue
                if platform == "instagram":
                    try:
                        name = graph(identity, token=page_token, params={"fields": "id,username"}).get("username", identity)
                    except ProviderError:
                        name = identity
                account = db.scalar(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                    PlatformAccount.platform == platform, PlatformAccount.account_id == identity))
                if not account:
                    account = PlatformAccount(brand_id=brand_id, platform=platform, account_id=identity)
                    db.add(account)
                account.token_encrypted, account.enabled = encrypt(page_token), platform not in selected
                account.config = {**common, "name": name or identity}
                selected.add(platform)
                connected += 1
        cursor = page.get("paging", {}).get("cursors", {}).get("after")
        if not page.get("data") or not cursor or cursor == after:
            break
        after = cursor
    record(db, "meta.connect_complete", brand_id, brand_id, admin.username, details={"accounts": connected})
    db.commit()
    return RedirectResponse(f"/?meta_connected={brand_id}&count={connected}", status_code=303)
