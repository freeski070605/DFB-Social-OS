import hashlib
import logging
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import meta_auth
from app.core.errors import ProviderError
from app.db.session import Base
from app.models import Admin, Brand, PlatformAccount, SystemSetting
from app.security import secrets as credential_secrets


@pytest.fixture
def callback_case(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    config = SimpleNamespace(meta_app_id="app", meta_app_secret="app-secret", meta_config_id="business-config",
                             meta_redirect_uri="https://admin.example/api/meta/callback",
                             meta_api_version="v23.0", encryption_key=Fernet.generate_key().decode())
    monkeypatch.setattr(meta_auth, "settings", lambda: config)
    monkeypatch.setattr(credential_secrets, "settings", lambda: config)
    request = SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(token_hash="session-hash")))
    with Session(engine) as db:
        admin = Admin(username="admin", password_hash="hash")
        db.add_all([admin, Brand(id=1, name="A", slug="a", config={})])
        db.commit()
        db.add(SystemSetting(key="meta_oauth:" + hashlib.sha256(b"private-state").hexdigest(),
                             value={"brand_id": 1, "admin_id": admin.id, "session_hash": "session-hash",
                                    "expires_at": "9999-01-01T00:00:00"}))
        db.commit()
        yield db, request, admin
    engine.dispose()


def metadata(token, *, stage="credential_validation"):
    return {"valid": True, "expires_at": None, "data_access_expires_at": None,
            "scopes": ["pages_show_list"], "token_type": "SYSTEM_USER" if token == "system-token" else "PAGE"}


def run_callback(case):
    db, request, admin = case
    return meta_auth.callback(request, code="private-code", state="private-state", admin=admin, db=db)


def test_business_callback_persists_encrypted_credentials(callback_case, monkeypatch, caplog):
    monkeypatch.setattr(meta_auth, "token_metadata", metadata)

    def graph(path, *, token=None, params=None, stage="graph_request"):
        if path == "oauth/access_token":
            return {"access_token": "system-token"}
        if path == "me/accounts":
            assert token == "system-token"
            return {"data": [{"id": "123", "name": "Page", "access_token": "page-token",
                              "instagram_business_account": {"id": "456"}}]}
        assert path == "456"
        return {"id": "456", "username": "ig"}

    monkeypatch.setattr(meta_auth, "graph", graph)
    with caplog.at_level(logging.INFO, logger="dfb.meta_callback"):
        result = run_callback(callback_case)
    assert result.status_code == 303
    db = callback_case[0]
    accounts = db.scalars(select(PlatformAccount)).all()
    assert [(a.platform, a.account_id, a.enabled) for a in accounts] == [
        ("facebook", "123", False), ("instagram", "456", False)]
    assert credential_secrets.decrypt(accounts[0].token_encrypted) == "page-token"
    assert accounts[1].token_encrypted == ""
    assert accounts[1].config["credential_account_id"] == accounts[0].id
    assert "database_persistence" in caplog.text
    assert not any(value in caplog.text for value in ("private-code", "private-state", "system-token", "page-token", "app-secret"))


@pytest.mark.parametrize(("failure_stage", "response", "expected"), [
    ("code_exchange", {"error": {"code": 190, "error_subcode": 123, "type": "OAuthException",
                                   "message": "Invalid code private-code"}}, "code_exchange"),
    ("credential_validation", None, "credential_validation"),
    ("page_discovery", {"error": {"code": 10, "type": "OAuthException", "message": "Permission denied"}}, "page_discovery"),
    ("instagram_identity", {"error": {"code": 100, "message": "IG unavailable"}}, "instagram_identity"),
    ("malformed_exchange", {"unexpected": True}, "no usable credential"),
    ("malformed_pages", {"data": {}}, "malformed Page data"),
])
def test_callback_failures_are_staged(callback_case, monkeypatch, caplog, failure_stage, response, expected):
    def graph(path, *, token=None, params=None, stage="graph_request"):
        if stage == failure_stage or (failure_stage == "malformed_exchange" and stage == "code_exchange") or (
            failure_stage == "malformed_pages" and stage == "page_discovery"
        ):
            if "error" in (response or {}):
                meta_auth.meta_error(stage, httpx.Response(400), response, ["private-code"])
            return response
        if path == "oauth/access_token":
            return {"access_token": "system-token"}
        if path == "me/accounts":
            return {"data": [{"id": "123", "instagram_business_account": {"id": "456"}}]}
        return {"id": "456", "username": "ig"}

    monkeypatch.setattr(meta_auth, "graph", graph)
    if failure_stage == "credential_validation":
        monkeypatch.setattr(meta_auth, "token_metadata", lambda token, **kwargs: (_ for _ in ()).throw(
            ProviderError("Meta credential_validation returned malformed credential data")))
    else:
        monkeypatch.setattr(meta_auth, "token_metadata", metadata)
    with caplog.at_level(logging.INFO, logger="dfb.meta_callback"), pytest.raises(ProviderError, match=expected):
        run_callback(callback_case)
    assert not any(value in caplog.text for value in ("private-code", "private-state", "system-token", "app-secret"))
    callback_case[0].rollback()
    assert callback_case[0].scalars(select(PlatformAccount)).all() == []


def test_zero_pages_is_success(callback_case, monkeypatch, caplog):
    monkeypatch.setattr(meta_auth, "token_metadata", metadata)
    monkeypatch.setattr(meta_auth, "graph", lambda path, **kwargs: (
        {"access_token": "system-token"} if path == "oauth/access_token" else {"data": []}))
    with caplog.at_level(logging.INFO, logger="dfb.meta_callback"):
        result = run_callback(callback_case)
    assert result.status_code == 303
    assert "count=0" in result.headers["location"]
    assert '"pages_discovered": 0' in caplog.text


def test_graph_error_preserves_safe_meta_details(monkeypatch, caplog):
    monkeypatch.setattr(meta_auth, "settings", lambda: SimpleNamespace(meta_api_version="v23.0", meta_app_secret="app-secret"))

    def respond(request):
        return httpx.Response(400, json={"error": {"type": "OAuthException", "code": 190, "error_subcode": 463,
                                                  "message": "Expired token private-token code private-code app-secret"}})

    client_type = httpx.Client
    monkeypatch.setattr(meta_auth.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(respond)))
    with caplog.at_level(logging.INFO, logger="dfb.meta_callback"), pytest.raises(ProviderError) as caught:
        meta_auth.graph("oauth/access_token", params={"code": "private-code", "client_secret": "app-secret"},
                        stage="code_exchange")
    detail = str(caught.value) + caplog.text
    assert "http_status=400" in detail and "error_code=190" in detail and "error_subcode=463" in detail
    assert not any(value in detail for value in ("private-code", "private-token", "app-secret"))


def test_malformed_debug_token_is_provider_error(monkeypatch, caplog):
    monkeypatch.setattr(meta_auth, "settings", lambda: SimpleNamespace(meta_app_id="app", meta_app_secret="secret"))
    monkeypatch.setattr(meta_auth, "graph", lambda *args, **kwargs: {"data": []})
    with caplog.at_level(logging.INFO, logger="dfb.meta_callback"), pytest.raises(ProviderError, match="malformed credential data"):
        meta_auth.token_metadata("private-token")
    assert '"stage": "credential_validation"' in caplog.text
    assert "private-token" not in caplog.text
