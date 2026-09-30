"""Facebook Login connection for Pages and Page-linked Instagram professional accounts."""
import hashlib
import json
import logging
import re
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
from app.accounts.credentials import encrypted_credential, has_credential
from app.accounts.capabilities import facebook_publishing_issue

router = APIRouter(prefix="/api", tags=["meta"])
log = logging.getLogger("dfb.meta_callback")


def diagnostic(stage, **fields):
    log.info("meta_callback %s", json.dumps({"stage": stage, **fields}, sort_keys=True))


def safe_meta_message(message, secrets_to_redact=()):
    if not isinstance(message, str):
        return ""
    for secret in secrets_to_redact:
        if secret:
            message = message.replace(str(secret), "[REDACTED]")
    message = re.sub(r"https?://\S+", "[REDACTED]", message)
    message = re.sub(r"(?i)\b(?:access_token|token|code|state|client_secret|secret)\s*(?:[=:]\s*|\s+)[^\s,;]+", "[REDACTED]", message)
    message = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "[REDACTED]", message)
    return re.sub(r"[^\w\s.,:;!?()'\-/\[\]]", "?", message[:300])


def meta_error(stage, response, result, secrets_to_redact=()):
    error = result.get("error") if isinstance(result, dict) else None
    error = error if isinstance(error, dict) else {}
    fields = {"http_status": response.status_code,
              "error_type": safe_meta_message(error.get("type", ""), secrets_to_redact),
              "error_code": error.get("code") if isinstance(error.get("code"), int) else None,
              "error_subcode": error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None,
              "error_message": safe_meta_message(error.get("message", ""), secrets_to_redact)}
    diagnostic(stage, outcome="upstream_error", **fields)
    detail = ", ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    raise ProviderError(f"Meta {stage} failed: {detail}")


def configured():
    cfg = settings()
    if not cfg.meta_app_id or not cfg.meta_app_secret or not cfg.meta_redirect_uri.startswith("https://"):
        raise DomainError("Set DFB_META_APP_ID, DFB_META_APP_SECRET, and an HTTPS DFB_META_REDIRECT_URI in .env")
    if not cfg.meta_config_id:
        raise DomainError("Set DFB_META_CONFIG_ID for Facebook Login for Business in .env")
    if not cfg.encryption_key:
        raise DomainError("Set DFB_ENCRYPTION_KEY before connecting Meta")
    return cfg


def graph(path, *, token=None, params=None, stage="graph_request"):
    cfg = settings()
    url = f"https://graph.facebook.com/{cfg.meta_api_version}/{path}"
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.get(url, params=params, headers={"Authorization": f"Bearer {token}"} if token else {})
            result = response.json()
    except httpx.HTTPError:
        diagnostic(stage, outcome="transport_error")
        raise ProviderError(f"Meta {stage} transport request failed") from None
    except ValueError:
        diagnostic(stage, outcome="malformed_json", http_status=response.status_code)
        raise ProviderError(f"Meta {stage} returned malformed JSON (HTTP {response.status_code})") from None
    if response.is_error or not isinstance(result, dict) or "error" in result:
        sensitive = [token, getattr(cfg, "meta_app_secret", "")]
        sensitive.extend(value for key, value in (params or {}).items() if key in {"code", "state", "input_token", "access_token", "client_secret"})
        meta_error(stage, response, result, sensitive)
    return result


def token_metadata(token, *, stage="credential_validation"):
    cfg = settings()
    if not cfg.meta_app_id or not cfg.meta_app_secret:
        raise DomainError("Set DFB_META_APP_ID and DFB_META_APP_SECRET to inspect Meta tokens")
    response = graph("debug_token", params={"input_token": token, "access_token": f"{cfg.meta_app_id}|{cfg.meta_app_secret}"}, stage=stage)
    result = response.get("data")
    if not isinstance(result, dict) or not isinstance(result.get("is_valid"), bool):
        diagnostic(stage, outcome="malformed_response")
        raise ProviderError(f"Meta {stage} returned malformed credential data")
    return {"valid": bool(result.get("is_valid")), "expires_at": result.get("expires_at") or None,
            "data_access_expires_at": result.get("data_access_expires_at") or None,
            "scopes": result.get("scopes", []), "token_type": result.get("type", "")}


def account_view(account):
    config = account.config or {}
    facebook_issue = facebook_publishing_issue(account) if account.platform == "facebook" else None
    publishing_status = ("UNHEALTHY" if not account.enabled or config.get("token_status") != "healthy"
                         or not config.get("last_checked") or not has_credential(account)
                         or (config.get("expires_at") and config["expires_at"] <= utcnow().timestamp())
                         or (config.get("data_access_expires_at") and config["data_access_expires_at"] <= utcnow().timestamp()) else
                         "ADDITIONAL_AUTHORIZATION_REQUIRED" if facebook_issue else "READY")
    return {"id": account.id, "platform": account.platform, "account_id": account.account_id,
            "enabled": account.enabled, "token_configured": has_credential(account),
            "name": config.get("name", ""), "source": config.get("source", "manual"),
            "permissions": config.get("permissions", []), "tasks": config.get("tasks", []),
            "token_status": config.get("token_status", "unchecked"),
            "expires_at": config.get("expires_at"), "data_access_expires_at": config.get("data_access_expires_at"),
            "token_type": config.get("token_type", ""),
            "last_checked": config.get("last_checked"),
            "publishing_status": publishing_status if account.platform == "facebook" else None,
            "publishing_reason": ("Credential is unhealthy; check connection" if publishing_status == "UNHEALTHY"
                                  else facebook_issue or "") if account.platform == "facebook" else ""}


def inspect_account(account):
    token = decrypt(encrypted_credential(account))
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


def linked_instagram_check(db, page_account):
    """Inspect one selected Page without changing its authorization or selection."""
    if page_account.platform != "facebook" or not page_account.enabled or not page_account.token_encrypted:
        raise DomainError("Select a connected Facebook Page first", 409)
    token = decrypt(page_account.token_encrypted)
    metadata = token_metadata(token, stage="linked_instagram_credential")
    scopes = sorted(metadata.get("scopes") or [])
    fields = "id,name,instagram_business_account"
    request_path = f"/{settings().meta_api_version}/{page_account.account_id}?fields={fields}"
    result = {"request": request_path, "http_status": None, "page_id": page_account.account_id,
              "page_name": (page_account.config or {}).get("name", ""), "granted_permissions": scopes,
              "instagram_business_account_exists": False, "instagram_id": None, "instagram_username": None,
              "error_type": None, "error_code": None, "error_subcode": None, "error_message": None,
              "capability": "UNDETERMINED", "registered": False}
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            response = client.get(f"https://graph.facebook.com/{settings().meta_api_version}/{page_account.account_id}",
                                  params={"fields": fields}, headers={"Authorization": f"Bearer {token}"})
        result["http_status"] = response.status_code
        body = response.json()
    except httpx.HTTPError:
        result["capability"] = "TRANSPORT_ERROR"
        return result
    except ValueError:
        result["capability"] = "MALFORMED_RESPONSE"
        return result
    if not isinstance(body, dict):
        result["capability"] = "MALFORMED_RESPONSE"
        return result
    if response.is_error or "error" in body:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        result.update(error_type=safe_meta_message(error.get("type"), (token,)),
                      error_code=error.get("code") if isinstance(error.get("code"), int) else None,
                      error_subcode=error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None,
                      error_message=safe_meta_message(error.get("message"), (token, settings().meta_app_secret)),
                      capability="ACCESS_DENIED" if response.status_code in (400, 401, 403) else "GRAPH_ERROR")
        return result
    if str(body.get("id")) != page_account.account_id or not isinstance(body.get("name"), str):
        result["capability"] = "MALFORMED_RESPONSE"
        return result
    result["page_name"] = body["name"]
    linked = body.get("instagram_business_account")
    if linked is None:
        result["capability"] = "PERMISSION_MISSING_OR_LINK_UNAVAILABLE" if "instagram_basic" not in scopes else "NO_API_LINK"
        return result
    if not isinstance(linked, dict) or not str(linked.get("id", "")).isdigit():
        result["capability"] = "MALFORMED_RESPONSE"
        return result
    ig_id = str(linked["id"])
    result.update(instagram_business_account_exists=True, instagram_id=ig_id)
    try:
        identity = graph(ig_id, token=token, params={"fields": "id,username"}, stage="linked_instagram_identity")
    except ProviderError:
        result["capability"] = "LINKED_IDENTITY_INACCESSIBLE"
        return result
    if str(identity.get("id")) != ig_id or not isinstance(identity.get("username"), str):
        result["capability"] = "MALFORMED_RESPONSE"
        return result
    result["instagram_username"] = identity["username"]
    result["capability"] = "DISCOVERABLE"
    if db.scalar(select(PlatformAccount.id).where(PlatformAccount.platform == "instagram",
                  PlatformAccount.account_id == ig_id, PlatformAccount.brand_id != page_account.brand_id)):
        result["capability"] = "ASSIGNED_TO_OTHER_BRAND"
        return result
    account = db.scalar(select(PlatformAccount).where(PlatformAccount.brand_id == page_account.brand_id,
                        PlatformAccount.platform == "instagram", PlatformAccount.account_id == ig_id))
    if account is None:
        account = PlatformAccount(brand_id=page_account.brand_id, platform="instagram", account_id=ig_id)
        db.add(account)
    account.token_encrypted = ""
    account.enabled = False if account.id is None else account.enabled
    account.config = {**(account.config or {}), "source": "oauth", "name": identity["username"],
                      "page_id": page_account.account_id, "credential_account_id": page_account.id,
                      "permissions": scopes,
                      "token_status": "healthy", "token_type": metadata.get("token_type", ""),
                      "expires_at": metadata.get("expires_at"),
                      "data_access_expires_at": metadata.get("data_access_expires_at"),
                      "last_checked": utcnow().isoformat() + "Z"}
    result["registered"] = True
    return result


@router.post("/brands/{brand_id}/accounts/{key}/linked-instagram/check")
def check_linked_instagram(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    page = db.scalar(select(PlatformAccount).where(PlatformAccount.id == key,
                     PlatformAccount.brand_id == brand_id, PlatformAccount.platform == "facebook"))
    if page is None:
        raise DomainError("Facebook Page account not found", 404)
    result = linked_instagram_check(db, page)
    record(db, "meta.linked_instagram_check", key, brand_id, admin.username,
           details={"capability": result["capability"], "registered": result["registered"]})
    db.commit()
    return result


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
              "response_type": "code", "override_default_response_type": "true",
              "config_id": cfg.meta_config_id}
    return {"authorization_url": "https://www.facebook.com/" + cfg.meta_api_version + "/dialog/oauth?" + urlencode(params)}


@router.get("/meta/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "", admin=Depends(authenticated), db=Depends(get_db)):
    diagnostic("session_validation", outcome="passed")
    cfg = configured()
    key = "meta_oauth:" + hashlib.sha256(state.encode()).hexdigest()
    pending = db.get(SystemSetting, key) if state else None
    if not pending or pending.value.get("admin_id") != admin.id or pending.value.get("session_hash") != request.state.session.token_hash or pending.value.get("expires_at", "") < utcnow().isoformat():
        diagnostic("state_validation", outcome="failed")
        raise DomainError("Meta connection expired or state validation failed. Start again from Settings.", 403)
    diagnostic("state_validation", outcome="passed")
    brand_id = pending.value["brand_id"]
    db.delete(pending)
    db.commit()  # The state is one use, including when Meta returns an error.
    diagnostic("state_consumed")
    if error or not code:
        diagnostic("authorization_result", outcome="denied_or_missing_code")
        raise DomainError("Meta authorization was cancelled or denied. Start connection again.", 400)
    # A system-user configuration returns the usable token directly from the code exchange.
    # The user-token long-lived exchange is not valid for this credential type.
    diagnostic("code_exchange", outcome="started")
    exchange = graph("oauth/access_token", params={"client_id": cfg.meta_app_id, "client_secret": cfg.meta_app_secret,
                                                   "redirect_uri": cfg.meta_redirect_uri, "code": code}, stage="code_exchange")
    credential = exchange.get("access_token")
    diagnostic("code_exchange", outcome="completed", credential_returned=bool(credential))
    if not isinstance(credential, str) or not credential:
        raise ProviderError("Meta code_exchange returned no usable credential")
    credential_meta = token_metadata(credential)
    diagnostic("credential_validation", outcome="completed", valid=credential_meta["valid"],
               credential_type=credential_meta["token_type"])
    if not credential_meta["valid"]:
        raise ProviderError("Meta returned an invalid credential. Reconnect this account.")
    granted = sorted(credential_meta.get("scopes", []))
    after = None
    connected = 0
    for _ in range(10):
        params = {"fields": "id,name,access_token,tasks,instagram_business_account", "limit": 100}
        if after:
            params["after"] = after
        diagnostic("page_discovery", outcome="started")
        page = graph("me/accounts", token=credential, params=params, stage="page_discovery")
        found_pages = page.get("data")
        if not isinstance(found_pages, list) or any(not isinstance(item, dict) for item in found_pages):
            diagnostic("page_discovery", outcome="malformed_response")
            raise ProviderError("Meta page_discovery returned malformed Page data")
        diagnostic("page_discovery", outcome="completed", pages_discovered=len(found_pages))
        for found in found_pages:
            page_id, page_token = str(found.get("id", "")), found.get("access_token") or credential
            if not page_id.isdigit():
                continue
            if not isinstance(page_token, str):
                diagnostic("page_token_retrieval", outcome="malformed_response")
                raise ProviderError("Meta page_token_retrieval returned malformed credential")
            diagnostic("page_token_retrieval", outcome="completed", credential_returned=True,
                       credential_source="page" if found.get("access_token") else "business_login")
            meta = token_metadata(page_token, stage="page_credential_validation")
            if not meta["valid"]:
                continue
            common = {"source": "oauth", "page_id": page_id, "permissions": meta.get("scopes", granted),
                      "tasks": found.get("tasks", []), "expires_at": meta["expires_at"],
                      "data_access_expires_at": meta["data_access_expires_at"], "token_status": "healthy",
                      "token_type": meta.get("token_type", ""),
                      "last_checked": utcnow().isoformat() + "Z"}
            linked = found.get("instagram_business_account")
            if linked is not None and not isinstance(linked, dict):
                diagnostic("instagram_discovery", outcome="malformed_response")
                raise ProviderError("Meta instagram_discovery returned malformed account data")
            diagnostic("instagram_discovery", outcome="completed", instagram_accounts_discovered=bool(linked and linked.get("id")))
            page_account_id = None
            for platform, identity, name in (("facebook", page_id, found.get("name", "")),
                                             ("instagram", str((linked or {}).get("id", "")), "")):
                if not identity or not identity.isdigit():
                    continue
                if db.scalar(select(PlatformAccount.id).where(PlatformAccount.platform == platform,
                    PlatformAccount.account_id == identity, PlatformAccount.brand_id != brand_id)):
                    continue
                if platform == "instagram" and page_account_id is None:
                    continue
                if platform == "instagram":
                    identity_data = graph(identity, token=page_token, params={"fields": "id,username"}, stage="instagram_identity")
                    if str(identity_data.get("id")) != identity:
                        diagnostic("instagram_identity", outcome="malformed_response")
                        raise ProviderError("Meta instagram_identity returned a different account ID")
                    name = identity_data.get("username") or identity
                account = db.scalar(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                    PlatformAccount.platform == platform, PlatformAccount.account_id == identity))
                if not account:
                    account = PlatformAccount(brand_id=brand_id, platform=platform, account_id=identity)
                    db.add(account)
                # Discovery never authorizes a publishing destination. The admin selects it explicitly.
                if platform == "facebook":
                    diagnostic("credential_encryption", outcome="started", platform=platform)
                    account.token_encrypted = encrypt(page_token)
                    diagnostic("credential_encryption", outcome="completed", platform=platform)
                    db.flush()
                    page_account_id = account.id
                else:
                    account.token_encrypted = ""
                account.enabled = False
                account.config = {**common, "name": name or identity,
                                  **({"credential_account_id": page_account_id} if platform == "instagram" else {})}
                connected += 1
        paging = page.get("paging") or {}
        if not isinstance(paging, dict) or not isinstance(paging.get("cursors") or {}, dict):
            diagnostic("page_discovery", outcome="malformed_paging")
            raise ProviderError("Meta page_discovery returned malformed paging data")
        cursor = (paging.get("cursors") or {}).get("after")
        if not page.get("data") or not cursor or cursor == after:
            break
        after = cursor
    if connected:
        for existing in db.scalars(select(PlatformAccount).where(PlatformAccount.brand_id == brand_id,
                                                                  PlatformAccount.platform.in_(["facebook", "instagram"]))):
            existing.enabled = False
    diagnostic("database_persistence", outcome="started", accounts_discovered=connected)
    record(db, "meta.connect_complete", brand_id, brand_id, admin.username, details={"accounts": connected})
    db.commit()
    diagnostic("database_persistence", outcome="completed", accounts_discovered=connected)
    diagnostic("final_redirect", outcome="completed", accounts_discovered=connected)
    return RedirectResponse(f"/?meta_connected={brand_id}&count={connected}", status_code=303)
