import hashlib
import logging
import secrets
from datetime import timedelta
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import Depends, Request
from sqlalchemy import select, delete
from app.core.config import settings
from app.core.errors import DomainError
from app.db.session import get_db, utcnow
from app.models import Admin, AdminSession, SystemSetting
from app.audit.service import record

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_hex(20))


def create_admin(db, username, password):
    if len(password) < 12 or len(password) > 1024:
        raise DomainError("Use a password of 12–1024 characters")
    if not username.strip() or len(username) > 80:
        raise DomainError("Username must be 1–80 characters")
    admin = Admin(username=username.strip(), password_hash=hasher.hash(password))
    db.add(admin)
    db.flush()
    record(db, "admin.create", admin.id, actor=username)
    return admin


def login(db, username, password, client):
    key = "login:" + hashlib.sha256(client.encode()).hexdigest()[:40]
    throttle = db.get(SystemSetting, key)
    now = utcnow().timestamp()
    entries = [v for v in (throttle.value.get("attempts", []) if throttle else []) if v > now - 900]
    if len(entries) >= 10:
        raise DomainError("Too many login attempts. Wait 15 minutes.", 429)
    if not throttle:
        throttle = SystemSetting(key=key)
        db.add(throttle)
    throttle.value = {"attempts": entries + [now]}
    db.commit()
    admin = db.scalar(select(Admin).where(Admin.username == username))
    try:
        hasher.verify(admin.password_hash if admin else DUMMY_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        record(db, "auth.login", actor="anonymous", result="DENIED")
        db.commit()
        raise DomainError("Invalid username or password", 401)
    if not admin:
        raise DomainError("Invalid username or password", 401)
    if hasher.check_needs_rehash(admin.password_hash):
        admin.password_hash = hasher.hash(password)
    db.execute(delete(AdminSession).where(AdminSession.expires_at < utcnow()))
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    db.add(AdminSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), admin_id=admin.id, csrf=csrf,
                        expires_at=utcnow() + timedelta(hours=settings().session_hours)))
    throttle.value = {"attempts": []}
    record(db, "auth.login", actor=admin.username)
    db.commit()
    return token, csrf, admin


def authenticated(request: Request, db=Depends(get_db)):
    token = request.cookies.get("dfb_session", "")
    session = db.get(AdminSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
    if not session or session.expires_at < utcnow():
        if getattr(getattr(request, "url", None), "path", None) == "/api/youtube/callback":
            state = getattr(request, "query_params", {}).get("state", "")
            logging.getLogger("dfb.youtube_oauth").info(
                "youtube_oauth_callback category=SESSION_MISSING cookie_present=%s session_record_found=%s state_hash=%s callback_host=%s browser_origin=%s",
                bool(token), bool(session), hashlib.sha256(state.encode()).hexdigest() if state else "unavailable",
                getattr(request.url, "hostname", "unavailable"),
                request.headers.get("x-dfb-public-origin", "unavailable"))
        raise DomainError("Sign in to continue", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise DomainError("Invalid session CSRF token", 403)
    request.state.session = session
    return db.get(Admin, session.admin_id)
