"""YouTube channel discovery and upload-capable OAuth scope handling."""
import hashlib
import json
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from app.audit.service import record
from app.core.config import settings, public_callback_url
from app.core.errors import DomainError, ProviderError
from app.db.session import get_db, utcnow
from app.models import Brand, PlatformAccount, SystemSetting
from app.repositories.common import require
from app.security.auth import authenticated
from app.security.secrets import encrypt, decrypt

router = APIRouter(prefix="/api", tags=["youtube"])
log = logging.getLogger("dfb.youtube_oauth")
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SCOPE = " ".join((READONLY_SCOPE, UPLOAD_SCOPE))


def granted_scopes(value):
    if not value:
        return set()
    if isinstance(value, str):
        items = value.split()
    else:
        items = list(value)
    return {item.strip() for item in items if item and str(item).strip()}


def youtube_account_state(account):
    config = account.config or {}
    scopes = set(config.get("permissions") or [])
    if not account.enabled or config.get("token_status") != "healthy" or not config.get("last_checked") or not config.get("expires_at") or config["expires_at"] <= utcnow().timestamp():
        return "CREDENTIAL UNHEALTHY"
    if UPLOAD_SCOPE not in scopes:
        return "READ-ONLY"
    return "PUBLISHING READY"


def configured():
    cfg = settings()
    redirect = public_callback_url("/api/youtube/callback", cfg.youtube_redirect_uri)
    if getattr(cfg, "public_origin", "") and redirect != cfg.public_origin.rstrip("/") + "/api/youtube/callback":
        raise DomainError("YouTube OAuth callback must use the configured public origin")
    if not cfg.youtube_client_id or not cfg.youtube_client_secret:
        raise DomainError("Configure YouTube OAuth client ID and secret first")
    if not cfg.encryption_key:
        raise DomainError("Configure encryption before connecting YouTube")
    cfg.youtube_redirect_uri = redirect
    return cfg


def diagnostic(stage, **fields):
    log.info("youtube_oauth %s", json.dumps({"stage": stage, **fields}, sort_keys=True))


def browser_origin(request):
    headers = getattr(request, "headers", {})
    return headers.get("x-dfb-public-origin", "")


def consume_state(db, pending, digest):
    result = db.execute(delete(SystemSetting).where(SystemSetting.key == pending.key))
    if result.rowcount != 1:
        db.rollback()
        return False
    db.add(SystemSetting(key="youtube_used:" + digest, value={
        "provider": "youtube", "consumed_at": utcnow().isoformat(),
        "created_at": pending.value.get("created_at"), "expires_at": pending.value.get("expires_at"),
        "session_hash": pending.value.get("session_hash"),
        "redirect_uri": pending.value.get("redirect_uri")}))
    db.commit()
    return True


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
    origin = browser_origin(request)
    if origin and origin != getattr(cfg, "public_origin", "").rstrip("/"):
        diagnostic("start", category="REDIRECT_MISMATCH", browser_origin=origin,
                   redirect_uri=cfg.youtube_redirect_uri)
        raise DomainError("YouTube OAuth must start from the production application", 403)
    state = secrets.token_urlsafe(32)
    now = utcnow()
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    db.add(SystemSetting(key="youtube_oauth:" + state_hash,
        value={"provider": "youtube", "brand_id": brand_id, "admin_id": admin.id,
               "session_hash": request.state.session.token_hash,
               "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat(),
               "redirect_uri": cfg.youtube_redirect_uri}))
    record(db, "youtube.connect_start", brand_id, brand_id, admin.username)
    db.commit()
    diagnostic("start", authenticated=True, browser_origin=origin or "unavailable",
               state_hash=state_hash, initiating_session_hash=request.state.session.token_hash,
               redirect_uri=cfg.youtube_redirect_uri, state_record_created=True,
               created_at=now.isoformat(), expires_at=(now + timedelta(minutes=10)).isoformat(),
               bound_admin_id=admin.id, session_bound=True, storage="sqlite_system_settings")
    params = {"client_id": cfg.youtube_client_id, "redirect_uri": cfg.youtube_redirect_uri,
              "response_type": "code", "scope": SCOPE, "state": state, "access_type": "offline",
              "include_granted_scopes": "true"}
    return {"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)}


@router.get("/youtube/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "",
             admin=Depends(authenticated), db=Depends(get_db)):
    cfg = configured()
    digest = hashlib.sha256(state.encode()).hexdigest()
    key = "youtube_oauth:" + digest
    pending = db.get(SystemSetting, key) if state else None
    used = db.get(SystemSetting, "youtube_used:" + digest) if state and not pending else None
    origin = browser_origin(request)
    value = pending.value if pending and isinstance(pending.value, dict) else {}
    prior = used.value if used and isinstance(used.value, dict) else {}
    callback_redirect = origin.rstrip("/") + "/api/youtube/callback" if origin else "unavailable"
    category = None
    if not pending:
        category = "STATE_ALREADY_CONSUMED" if used else "STATE_NOT_FOUND"
    elif not all(value.get(field) for field in ("admin_id", "session_hash", "expires_at", "brand_id")):
        category = "OTHER_VALIDATION_FAILURE"
    elif value.get("provider", "youtube") != "youtube":
        category = "OTHER_VALIDATION_FAILURE"
    elif value["expires_at"] < utcnow().isoformat():
        category = "STATE_EXPIRED"
    elif value.get("admin_id") != admin.id or value.get("session_hash") != request.state.session.token_hash:
        category = "SESSION_MISMATCH"
    elif value.get("redirect_uri", cfg.youtube_redirect_uri) != cfg.youtube_redirect_uri or (origin and origin != getattr(cfg, "public_origin", "").rstrip("/")):
        category = "REDIRECT_MISMATCH"
    if category:
        diagnostic("callback_validation", category=category, authenticated=True,
                   callback_host=getattr(getattr(request, "url", None), "hostname", "unavailable"),
                   browser_origin=origin or "unavailable", state_hash=digest,
                   callback_session_hash=request.state.session.token_hash,
                   state_record_found=bool(pending),
                   state_created_at=value.get("created_at", prior.get("created_at")),
                   state_expires_at=value.get("expires_at", prior.get("expires_at")),
                   state_expired=bool(value.get("expires_at") and value["expires_at"] < utcnow().isoformat()),
                   state_consumed=bool(used), bound_admin_id=value.get("admin_id"),
                   session_binding_matched=(value.get("session_hash", prior.get("session_hash")) == request.state.session.token_hash) if pending or used else None,
                   stored_redirect_uri=value.get("redirect_uri", prior.get("redirect_uri")),
                   callback_redirect_uri=callback_redirect, expected_redirect_uri=cfg.youtube_redirect_uri)
        raise DomainError("YouTube connection expired or state validation failed", 403)
    brand_id = pending.value["brand_id"]
    diagnostic("callback_validation", category="STATE_VALID", authenticated=True,
               callback_host=getattr(getattr(request, "url", None), "hostname", "unavailable"),
               browser_origin=origin or "unavailable", state_hash=digest,
               callback_session_hash=request.state.session.token_hash,
               state_record_found=True, state_created_at=value.get("created_at"),
               state_expires_at=value.get("expires_at"), state_expired=False, state_consumed=False,
               bound_admin_id=value.get("admin_id"), session_binding_matched=True,
               stored_redirect_uri=value.get("redirect_uri"), callback_redirect_uri=callback_redirect,
               expected_redirect_uri=cfg.youtube_redirect_uri)
    if not consume_state(db, pending, digest):
        diagnostic("callback_validation", category="STATE_ALREADY_CONSUMED", authenticated=True,
                   state_hash=digest, callback_session_hash=request.state.session.token_hash)
        raise DomainError("YouTube connection expired or state validation failed", 403)
    diagnostic("state_consumed", state_hash=digest)
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
        diagnostic("code_exchange", state_hash=digest, outcome="failed", error_type=type(exc).__name__)
        raise ProviderError("YouTube token exchange failed") from exc
    diagnostic("code_exchange", state_hash=digest, outcome="completed")
    granted = granted_scopes(token.get("scope", ""))
    if not token.get("access_token") or (READONLY_SCOPE not in granted and UPLOAD_SCOPE not in granted):
        raise ProviderError("YouTube channel read permission was not granted")
    found = channels(token["access_token"])
    diagnostic("channel_discovery", state_hash=digest, outcome="completed", count=len(found))
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
            "permissions": sorted(granted), "token_status": "healthy",
            "expires_at": int(utcnow().timestamp()) + int(token.get("expires_in", 0)),
            "refresh_token_encrypted": encrypt(token["refresh_token"]) if token.get("refresh_token") else (account.config or {}).get("refresh_token_encrypted"),
            "last_checked": utcnow().isoformat() + "Z"}
        count += 1
    record(db, "youtube.connect_complete", brand_id, brand_id, admin.username, details={"channels": count})
    db.commit()
    diagnostic("connection_complete", state_hash=digest, channels=count)
    return RedirectResponse(f"/?youtube_connected={brand_id}&count={count}", status_code=303)
