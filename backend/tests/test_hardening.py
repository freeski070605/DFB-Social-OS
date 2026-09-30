import hashlib
import json
import logging
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import meta_auth
from app.audit import service as audit
from app.core.config import Settings
from app.core.logging import MetaCallbackAccessFilter
from app.security import secrets as credential_secrets
from app.core.errors import DomainError, ProviderError
from app.db.session import Base
from app.models import Admin, Brand, PlatformAccount, SystemSetting
from app.publishing import providers
from app.publishing.public_media import LocalUnavailablePublicMediaProvider


def test_meta_config_id_uses_settings_environment(monkeypatch):
    monkeypatch.setenv("DFB_META_CONFIG_ID", "business-configuration-id")
    assert Settings(_env_file=None).meta_config_id == "business-configuration-id"


def test_meta_business_login_exchanges_and_discovers_without_selecting_or_publishing(monkeypatch, caplog):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    config = SimpleNamespace(meta_app_id="app", meta_app_secret="app-secret", meta_config_id="business-config",
                             meta_redirect_uri="https://admin.example/api/meta/callback",
                             meta_api_version="v23.0", encryption_key=Fernet.generate_key().decode())
    monkeypatch.setattr(meta_auth, "settings", lambda: config)
    monkeypatch.setattr(credential_secrets, "settings", lambda: config)
    monkeypatch.setattr(meta_auth, "encrypt", credential_secrets.encrypt)
    calls = []

    def metadata(token, *, stage="credential_validation"):
        calls.append(("debug_token", token))
        return {"valid": True, "expires_at": None, "data_access_expires_at": None,
                "scopes": ["pages_show_list"], "token_type": "SYSTEM_USER"}

    monkeypatch.setattr(meta_auth, "token_metadata", metadata)

    def fake_graph(path, *, token=None, params=None, stage="graph_request"):
        calls.append((path, token, params))
        if path == "oauth/access_token":
            assert params == {"client_id": "app", "client_secret": "app-secret",
                              "redirect_uri": config.meta_redirect_uri, "code": "authorization-code"}
            return {"access_token": "system-user-token"}
        if path == "me/accounts":
            assert token == "system-user-token"
            return {"data": [{"id": "123", "name": "Page A", "access_token": "page-token-a",
                              "instagram_business_account": {"id": "456"}},
                             {"id": "789", "name": "Page B"}]}
        if path == "456":
            return {"id": "456", "username": "ig_a"}
        raise AssertionError(path)

    monkeypatch.setattr(meta_auth, "graph", fake_graph)
    request = SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(token_hash="session-hash")))
    with Session(engine) as db:
        admin = Admin(username="admin", password_hash="hash")
        db.add_all([admin, Brand(id=1, name="A", slug="a", config={}), Brand(id=2, name="B", slug="b", config={})])
        db.commit()
        url = meta_auth.start_connection(1, request, admin=admin, db=db)["authorization_url"]
        query = parse_qs(urlparse(url).query)
        assert query["config_id"] == ["business-config"]
        assert query["response_type"] == ["code"]
        assert query["override_default_response_type"] == ["true"]
        assert "scope" not in query
        state = query["state"][0]
        with pytest.raises(DomainError, match="state validation failed"):
            meta_auth.callback(SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(token_hash="other"))),
                               code="authorization-code", state=state, admin=admin, db=db)
        result = meta_auth.callback(request, code="authorization-code", state=state, admin=admin, db=db)
        assert result.status_code == 303
        accounts = db.scalars(select(PlatformAccount).order_by(PlatformAccount.id)).all()
        assert [(a.platform, a.account_id, a.enabled) for a in accounts] == [
            ("facebook", "123", False), ("instagram", "456", False), ("facebook", "789", False)]
        assert all(a.brand_id == 1 for a in accounts)
        assert [credential_secrets.decrypt(a.token_encrypted) for a in (accounts[0], accounts[2])] == [
            "page-token-a", "system-user-token"]
        assert accounts[1].token_encrypted == ""
        assert accounts[1].config["credential_account_id"] == accounts[0].id
        assert [a.config["name"] for a in accounts] == ["Page A", "ig_a", "Page B"]
        assert all(a.config["token_status"] == "healthy" for a in accounts)
        assert [c[0] for c in calls].count("oauth/access_token") == 1
        assert not any(c[0] in {"me/permissions", "fb_exchange_token"} for c in calls)
        assert not any(c[0] in {"123/feed", "456/media", "456/media_publish"} for c in calls)
        assert not any(secret in caplog.text for secret in ["app-secret", "authorization-code", "system-user-token", "page-token-a"])
        assert not db.scalars(select(SystemSetting).where(SystemSetting.key.like("meta_oauth:%"))).first()
        with pytest.raises(DomainError, match="state validation failed"):
            meta_auth.callback(request, code="authorization-code", state=state, admin=admin, db=db)
    engine.dispose()


def test_meta_graph_rejects_provider_error_without_logging_credentials(monkeypatch, caplog):
    monkeypatch.setattr(meta_auth, "settings", lambda: SimpleNamespace(meta_api_version="v23.0"))

    class FailedClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert kwargs["params"]["client_secret"] == "private-app-secret"
            return httpx.Response(400, json={"error": {"message": "secret private-app-secret code private-code"}})

    monkeypatch.setattr(meta_auth.httpx, "Client", FailedClient)
    with pytest.raises(ProviderError, match="Meta code_exchange failed") as caught:
        meta_auth.graph("oauth/access_token", params={"client_secret": "private-app-secret", "code": "private-code"}, stage="code_exchange")
    assert "private-app-secret" not in str(caught.value) + caplog.text
    assert "private-code" not in str(caught.value) + caplog.text


def test_meta_callback_access_log_redacts_authorization_code():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/api/meta/callback?code=private-code&state=private-state", "1.1", 303), None)
    assert MetaCallbackAccessFilter().filter(record)
    assert "private-code" not in record.getMessage()
    assert "private-state" not in record.getMessage()


def test_public_media_fails_closed_for_approved_images_only(tmp_path, monkeypatch):
    from app.publishing import public_media
    from app.storage.local import LocalStorage

    data = b"rendered-image"
    digest = hashlib.sha256(data).hexdigest()
    asset = {"key": f"brand/1/r1-1-{digest[:24]}.png", "sha256": digest}
    LocalStorage(tmp_path).write(asset["key"], data)
    monkeypatch.setattr(public_media, "LocalStorage", lambda: LocalStorage(tmp_path))
    content = SimpleNamespace(id=1, revision=1, status="APPROVED", assets=[asset])
    provider = LocalUnavailablePublicMediaProvider()
    with pytest.raises(ProviderError, match="unavailable"):
        provider.prepare(content, asset)
    with pytest.raises(ProviderError, match="Only approved rendered"):
        provider.prepare(content, {"key": "../../secrets.png"})


def test_uncertain_external_request_cannot_be_treated_as_transient(monkeypatch):
    class BrokenClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(providers.httpx, "Client", BrokenClient)
    monkeypatch.setattr(providers, "decrypt", lambda value: "token")
    monkeypatch.setattr(providers, "settings", lambda: SimpleNamespace(meta_api_version="v23.0"))
    with pytest.raises(ProviderError) as caught:
        providers.MetaPublisher().request(SimpleNamespace(token_encrypted="encrypted"), "POST", "123/feed", outward=True)
    assert caught.value.uncertain and not caught.value.transient


def test_audit_redacts_nested_credentials(monkeypatch):
    monkeypatch.setattr(audit, "settings", lambda: SimpleNamespace(encryption_key="key-secret", meta_app_secret="app-secret",
        meta_verify_token="verify-secret", s3_access_key="access-secret", s3_secret_key="storage-secret"))
    assert audit.safe({"token": "raw-token", "nested": {"secret_key": "storage-secret"},
                       "reason": "Bearer leaked-token"}) == {
        "token": "[REDACTED]", "nested": {"secret_key": "[REDACTED]"}, "reason": "Bearer [REDACTED]"}


def test_community_reconciliation_requires_verified_outcome():
    from app.api.operations import CommunityAction, community_action
    from app.models import Interaction

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = Admin(username="admin", password_hash="hash")
        db.add_all([admin, Brand(id=1, name="A", slug="a", config={}),
                    Interaction(id=1, brand_id=1, platform="facebook", external_id="comment", body="body",
                                status="UNKNOWN", action="MODERATE_HIDE")])
        db.commit()
        with pytest.raises(DomainError, match="Confirm whether"):
            community_action(1, 1, CommunityAction(action="reconcile", reason="checked"), admin=admin, db=db)
        result = community_action(1, 1, CommunityAction(action="reconcile", reason="checked on Meta",
            confirmed_applied=True), admin=admin, db=db)
        assert result["status"] == "CLOSED"
    engine.dispose()


def test_backup_restores_media_and_clears_sessions(tmp_path, monkeypatch):
    from app.services import backup as backups
    from app.storage.local import LocalStorage

    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE system_settings (key TEXT, value TEXT);"
                         "CREATE TABLE admin_sessions (token_hash TEXT);"
                         "CREATE TABLE brands (id INTEGER, paused INTEGER);")
        db.execute("INSERT INTO system_settings VALUES (?, ?)", ("autopilot", json.dumps({"paused": False})))
        db.execute("INSERT INTO system_settings VALUES (?, ?)", ("meta_oauth:pending", "{}"))
        db.execute("INSERT INTO admin_sessions VALUES ('session')")
        db.execute("INSERT INTO brands VALUES (1, 0)")
    media = LocalStorage(tmp_path / "media")
    media.write("brand/1/image.png", b"image")
    brand_file = tmp_path / "brands" / "a" / "brand.json"
    brand_file.parent.mkdir(parents=True)
    brand_file.write_text("{}")
    monkeypatch.setattr(backups, "ROOT", tmp_path)
    monkeypatch.setattr(backups, "settings", lambda: SimpleNamespace(database_url=f"sqlite:///{database}"))
    monkeypatch.setattr(backups, "LocalStorage", lambda: media)
    archive = backups.backup()
    media.path("brand/1/image.png").unlink()
    brand_file.unlink()
    backups.restore(archive)
    assert media.read("brand/1/image.png") == b"image"
    assert brand_file.read_text() == "{}"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM system_settings WHERE key LIKE 'meta_oauth:%'").fetchone()[0] == 0
        assert json.loads(db.execute("SELECT value FROM system_settings WHERE key='autopilot'").fetchone()[0])["paused"]


def test_existing_duplicate_meta_assignment_fails_closed():
    from app.publishing.service import account_for

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Brand(id=1, name="A", slug="a", config={}), Brand(id=2, name="B", slug="b", config={}),
                    PlatformAccount(brand_id=1, platform="facebook", account_id="123", token_encrypted="encrypted", enabled=True),
                    PlatformAccount(brand_id=2, platform="facebook", account_id="123", token_encrypted="encrypted", enabled=True)])
        db.commit()
        with pytest.raises(DomainError, match="another brand"):
            account_for(db, 1, "facebook")
    engine.dispose()
