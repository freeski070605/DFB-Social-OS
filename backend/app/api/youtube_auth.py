"""Read-only YouTube channel discovery. Upload permission is deliberately not requested."""
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

router = APIRouter(prefix="/api", tags=["youtube"])
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


def configured():
    cfg = settings()
    if not cfg.youtube_client_id or not cfg.youtube_client_secret or not cfg.youtube_redirect_uri:
        raise DomainError("Configure YouTube OAuth client ID, secret and redirect URI first")
    if not cfg.encryption_key:
        raise DomainError("Configure encryption before connecting YouTube")
    return cfg


def channels(token):
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.get("https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            return response.json().get("items", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("YouTube channel verification failed. Reconnect and try again.") from exc


def inspect_account(account):
    if (account.config or {}).get("expires_at", 0) <= utcnow().timestamp():
        raise ProviderError("YouTube access token expired. Reconnect to verify the channel")
    found = channels(decrypt(account.token_encrypted))
    match = next((row for row in found if row.get("id") == account.account_id), None)
    if not match:
        raise ProviderError("The authorized Google account no longer exposes this channel")
    account.config = {**(account.config or {}), "name": match.get("snippet", {}).get("title", ""),
                      "token_status": "healthy", "last_checked": utcnow().isoformat() + "Z"}
    return account


@router.post("/brands/{brand_id}/youtube/connect")
def start(brand_id: int, request: Request, admin=Depends(authenticated), db=Depends(get_db)):
    cfg = configured()
    require(db, Brand, brand_id)
    state = secrets.token_urlsafe(32)
    db.add(SystemSetting(key="youtube_oauth:" + hashlib.sha256(state.encode()).hexdigest(),
        value={"brand_id": brand_id, "admin_id": admin.id, "session_hash": request.state.session.token_hash,
               "expires_at": (utcnow() + timedelta(minutes=10)).isoformat()}))
    record(db, "youtube.connect_start", brand_id, brand_id, admin.username)
    db.commit()
    params = {"client_id": cfg.youtube_client_id, "redirect_uri": cfg.youtube_redirect_uri,
              "response_type": "code", "scope": SCOPE, "state": state, "access_type": "offline",
              "include_granted_scopes": "true"}
    return {"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)}


@router.get("/youtube/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             admin=Depends(authenticated), db=Depends(get_db)):
    cfg = configured()
    key = "youtube_oauth:" + hashlib.sha256(state.encode()).hexdigest()
    pending = db.get(SystemSetting, key) if state else None
    if not pending or pending.value.get("admin_id") != admin.id or pending.value.get("session_hash") != request.state.session.token_hash or pending.value.get("expires_at", "") < utcnow().isoformat():
        raise DomainError("YouTube connection expired or state validation failed", 403)
    brand_id = pending.value["brand_id"]
    db.delete(pending)
    db.commit()
    if error or not code:
        raise DomainError("YouTube authorization was denied or cancelled")
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": cfg.youtube_client_id, "client_secret": cfg.youtube_client_secret,
                "redirect_uri": cfg.youtube_redirect_uri, "grant_type": "authorization_code"})
            response.raise_for_status()
            token = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("YouTube token exchange failed") from exc
    if SCOPE not in token.get("scope", "").split() or not token.get("access_token"):
        raise ProviderError("YouTube channel read permission was not granted")
    found = channels(token["access_token"])
    count = 0
    for row in found:
        channel_id = str(row.get("id", ""))
        if not channel_id:
            continue
        if db.scalar(select(PlatformAccount.id).where(PlatformAccount.platform == "youtube",
            PlatformAccount.account_id == channel_id, PlatformAccount.brand_id != brand_id)):
            continue
        account = db.scalar(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
            PlatformAccount.platform == "youtube", PlatformAccount.account_id == channel_id))
        if not account:
            account = PlatformAccount(brand_id=brand_id, platform="youtube", account_id=channel_id)
            db.add(account)
        account.enabled = False
        account.token_encrypted = encrypt(token["access_token"])
        account.config = {"source": "oauth", "name": row.get("snippet", {}).get("title", ""),
            "permissions": [SCOPE], "token_status": "healthy",
            "expires_at": int(utcnow().timestamp()) + int(token.get("expires_in", 0)),
            "refresh_token_encrypted": encrypt(token["refresh_token"]) if token.get("refresh_token") else (account.config or {}).get("refresh_token_encrypted"),
            "last_checked": utcnow().isoformat() + "Z"}
        count += 1
    record(db, "youtube.connect_complete", brand_id, brand_id, admin.username, details={"channels": count})
    db.commit()
    return RedirectResponse(f"/?youtube_connected={brand_id}&count={count}", status_code=303)
