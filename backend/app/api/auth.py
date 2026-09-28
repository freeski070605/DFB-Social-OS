from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete
from app.db.session import get_db
from app.security.auth import authenticated, login, hasher
from app.models import AdminSession
from app.core.config import settings
from app.core.errors import DomainError
from app.audit.service import record

router = APIRouter(prefix="/api/auth", tags=["authentication"])


class LoginInput(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=1024)


@router.post("/login")
def sign_in(data: LoginInput, request: Request, response: Response, db=Depends(get_db)):
    token, csrf, admin = login(db, data.username, data.password, request.client.host)
    response.set_cookie("dfb_session", token, httponly=True, secure=settings().cookie_secure, samesite="lax", max_age=settings().session_hours * 3600, path="/")
    return {"username": admin.username, "csrf": csrf}


@router.get("/me")
def me(request: Request, admin=Depends(authenticated)):
    return {"username": admin.username, "csrf": request.state.session.csrf}


@router.post("/logout")
def logout(request: Request, response: Response, admin=Depends(authenticated), db=Depends(get_db)):
    db.execute(delete(AdminSession).where(AdminSession.token_hash == request.state.session.token_hash))
    record(db, "auth.logout", actor=admin.username)
    db.commit()
    response.delete_cookie("dfb_session", path="/")
    return {"ok": True}


class PasswordInput(BaseModel):
    current: str = Field(max_length=1024)
    password: str = Field(min_length=12, max_length=1024)


@router.post("/password")
def password(data: PasswordInput, admin=Depends(authenticated), db=Depends(get_db)):
    from argon2.exceptions import VerifyMismatchError
    try:
        hasher.verify(admin.password_hash, data.current)
    except VerifyMismatchError:
        raise DomainError("Current password is incorrect", 403)
    admin.password_hash = hasher.hash(data.password)
    db.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
    record(db, "auth.password_change", actor=admin.username)
    db.commit()
    return {"ok": True, "message": "Password changed. Sign in again."}
