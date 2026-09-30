import hashlib
import logging
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import youtube_auth
from app.core.errors import DomainError
from app.core.logging import MetaCallbackAccessFilter
from app.db.session import Base, utcnow
from app.models import Brand, PlatformAccount, SystemSetting
from app.security.auth import authenticated


@pytest.fixture
def oauth(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth.sqlite3'}")
    Base.metadata.create_all(engine)
    cfg = SimpleNamespace(youtube_client_id="client", youtube_client_secret="secret",
        youtube_redirect_uri="https://dfb-social-os.vercel.app/api/youtube/callback",
        public_origin="https://dfb-social-os.vercel.app", encryption_key="key")
    monkeypatch.setattr(youtube_auth, "configured", lambda: cfg)
    monkeypatch.setattr(youtube_auth, "channels", lambda token: [{"id": "channel", "snippet": {"title": "Channel"}}])
    monkeypatch.setattr(youtube_auth, "encrypt", lambda value: "encrypted")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600,
                    "scope": youtube_auth.SCOPE}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert url == "https://oauth2.googleapis.com/token"
            return Response()

    monkeypatch.setattr(youtube_auth.httpx, "Client", Client)
    with Session(engine) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.commit()
    yield engine, cfg
    engine.dispose()


def request(session="session", origin="https://dfb-social-os.vercel.app"):
    return SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(token_hash=session)),
        headers={"x-dfb-public-origin": origin}, url=SimpleNamespace(hostname="tunnel.example"))


def begin(db, admin):
    url = youtube_auth.start(1, request(), admin=admin, db=db)["authorization_url"]
    query = parse_qs(urlparse(url).query)
    return query["state"][0], query["redirect_uri"][0]


def test_fresh_state_survives_new_database_session_and_replay_is_rejected(oauth, caplog):
    engine, cfg = oauth
    admin = SimpleNamespace(id=1, username="owner")
    with Session(engine) as db:
        state, redirect = begin(db, admin)
        assert redirect == cfg.youtube_redirect_uri
        stored = db.get(SystemSetting, "youtube_oauth:" + hashlib.sha256(state.encode()).hexdigest())
        assert stored.value["provider"] == "youtube"
        assert stored.value["session_hash"] == "session"
        assert stored.value["redirect_uri"] == redirect
        assert stored.value["created_at"] < stored.value["expires_at"]
    database = Path(engine.url.database)
    engine.dispose()
    restarted_engine = create_engine(f"sqlite:///{database}")
    with Session(restarted_engine) as db, caplog.at_level(logging.INFO, logger="dfb.youtube_oauth"):
        result = youtube_auth.callback(request(), code="private-authorization-code", state=state, admin=admin, db=db)
        assert result.status_code == 303
        assert "STATE_VALID" in caplog.text
        with pytest.raises(DomainError):
            youtube_auth.callback(request(), code="private-authorization-code", state=state, admin=admin, db=db)
        assert "STATE_ALREADY_CONSUMED" in caplog.text
        assert state not in caplog.text and "private-authorization-code" not in caplog.text
        assert not db.get(SystemSetting, "youtube_oauth:" + hashlib.sha256(state.encode()).hexdigest())
        assert db.get(SystemSetting, "youtube_used:" + hashlib.sha256(state.encode()).hexdigest())
        assert db.scalar(select(PlatformAccount).where(PlatformAccount.platform == "youtube"))
    restarted_engine.dispose()


@pytest.mark.parametrize("change,category", [
    ("expired", "STATE_EXPIRED"), ("unknown", "STATE_NOT_FOUND"),
    ("session", "SESSION_MISMATCH"), ("redirect", "REDIRECT_MISMATCH"),
])
def test_invalid_state_is_classified_and_existing_credential_unchanged(oauth, caplog, change, category):
    engine, _ = oauth
    admin = SimpleNamespace(id=1, username="owner")
    with Session(engine) as db:
        db.add(PlatformAccount(brand_id=1, platform="youtube", account_id="existing",
            enabled=True, token_encrypted="existing-encrypted", config={"refresh_token_encrypted": "existing-refresh"}))
        db.commit()
        state, _ = begin(db, admin)
        pending = db.scalar(select(SystemSetting).where(SystemSetting.key.like("youtube_oauth:%")))
        if change == "expired":
            pending.value = {**pending.value, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
        if change == "redirect":
            pending.value = {**pending.value, "redirect_uri": "https://preview.example/api/youtube/callback"}
        db.commit()
    with Session(engine) as db, caplog.at_level(logging.INFO, logger="dfb.youtube_oauth"):
        with pytest.raises(DomainError):
            youtube_auth.callback(request("other" if change == "session" else "session"),
                code="code", state="unknown" if change == "unknown" else state, admin=admin, db=db)
        assert category in caplog.text
        existing = db.scalar(select(PlatformAccount).where(PlatformAccount.account_id == "existing"))
        assert existing.enabled and existing.token_encrypted == "existing-encrypted"
        assert existing.config["refresh_token_encrypted"] == "existing-refresh"
        if change != "unknown":
            assert db.scalar(select(SystemSetting).where(SystemSetting.key.like("youtube_oauth:%")))
            if change == "session":
                assert youtube_auth.callback(request(), code="code", state=state, admin=admin, db=db).status_code == 303
            else:
                with pytest.raises(DomainError):
                    youtube_auth.callback(request(), code="code", state=state, admin=admin, db=db)
                assert db.scalar(select(SystemSetting).where(SystemSetting.key.like("youtube_oauth:%")))
            assert "STATE_ALREADY_CONSUMED" not in caplog.text


def test_consumed_state_rejected_without_provider_request(oauth, monkeypatch, caplog):
    engine, _ = oauth
    admin = SimpleNamespace(id=1, username="owner")
    with Session(engine) as db:
        state, _ = begin(db, admin)
        pending = db.scalar(select(SystemSetting).where(SystemSetting.key.like("youtube_oauth:%")))
        youtube_auth.consume_state(db, pending, pending.key.removeprefix("youtube_oauth:"))
        with caplog.at_level(logging.INFO, logger="dfb.youtube_oauth"):
            with pytest.raises(DomainError):
                youtube_auth.callback(request(), code="code", state=state, admin=admin, db=db)
        assert "STATE_ALREADY_CONSUMED" in caplog.text


def test_callback_missing_session_is_classified_without_exposing_cookie(caplog):
    incoming = SimpleNamespace(cookies={}, method="GET", headers={},
        url=SimpleNamespace(path="/api/youtube/callback", hostname="tunnel.example"), state=SimpleNamespace())
    with caplog.at_level(logging.INFO, logger="dfb.youtube_oauth"):
        with pytest.raises(DomainError, match="Sign in"):
            authenticated(incoming, db=SimpleNamespace(get=lambda *args: None))
    assert "SESSION_MISSING" in caplog.text


def test_failed_validation_does_not_consume_unrelated_state(oauth):
    engine, _ = oauth
    admin = SimpleNamespace(id=1, username="owner")
    with Session(engine) as db:
        first, _ = begin(db, admin)
        second, _ = begin(db, admin)
        with pytest.raises(DomainError):
            youtube_auth.callback(request("wrong"), code="code", state=first, admin=admin, db=db)
        assert db.scalar(select(SystemSetting).where(SystemSetting.key ==
            "youtube_oauth:" + hashlib.sha256(second.encode()).hexdigest()))


def test_start_rejects_preview_origin(oauth):
    engine, _ = oauth
    with Session(engine) as db:
        with pytest.raises(DomainError):
            youtube_auth.start(1, request(origin="https://preview.vercel.app"),
                admin=SimpleNamespace(id=1, username="owner"), db=db)
        assert not db.scalar(select(SystemSetting).where(SystemSetting.key.like("youtube_oauth:%")))


def test_youtube_callback_access_log_removes_code_and_state():
    access = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/api/youtube/callback?code=private-code&state=private-state", "1.1", 403), None)
    assert MetaCallbackAccessFilter().filter(access)
    assert "private-code" not in access.getMessage()
    assert "private-state" not in access.getMessage()
