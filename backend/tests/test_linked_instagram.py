import logging
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import meta_auth
from app.accounts.credentials import encrypted_credential
from app.accounts.capabilities import provider_view
from app.db.session import Base
from app.models import Brand, PlatformAccount
from app.security import secrets as credential_secrets


@pytest.fixture
def page_case(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    config = SimpleNamespace(meta_app_id="app", meta_app_secret="app-secret", meta_api_version="v23.0",
                             encryption_key=Fernet.generate_key().decode())
    monkeypatch.setattr(meta_auth, "settings", lambda: config)
    monkeypatch.setattr(credential_secrets, "settings", lambda: config)
    monkeypatch.setattr(meta_auth, "token_metadata", lambda token, **kwargs: {
        "valid": True, "scopes": ["pages_show_list", "pages_read_engagement"],
        "token_type": "PAGE", "expires_at": None, "data_access_expires_at": None})
    with Session(engine) as db:
        db.add(Brand(id=1, name="A", slug="a", config={}))
        page = PlatformAccount(brand_id=1, platform="facebook", account_id="123", enabled=True,
                               token_encrypted=credential_secrets.encrypt("private-page-token"),
                               config={"name": "Page", "token_status": "healthy"})
        db.add(page)
        db.commit()
        yield db, page
    engine.dispose()


def mock_page(monkeypatch, status, body):
    original = httpx.Client
    def respond(request):
        assert request.headers["Authorization"] == "Bearer private-page-token"
        assert request.url.params["fields"] == "id,name,instagram_business_account"
        return httpx.Response(status, json=body)
    monkeypatch.setattr(meta_auth.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond)))


def test_linked_page_registers_without_changing_page_credential_or_selection(page_case, monkeypatch):
    db, page = page_case
    mock_page(monkeypatch, 200, {"id": "123", "name": "Page", "instagram_business_account": {"id": "456"}})
    monkeypatch.setattr(meta_auth, "graph", lambda path, **kwargs: {"id": "456", "username": "life"})
    first = meta_auth.linked_instagram_check(db, page)
    db.commit()
    second = meta_auth.linked_instagram_check(db, page)
    db.commit()
    ig = db.scalars(select(PlatformAccount).where(PlatformAccount.platform == "instagram")).all()
    assert first["capability"] == second["capability"] == "DISCOVERABLE"
    assert first["registered"] and len(ig) == 1
    assert ig[0].config["page_id"] == page.account_id and not ig[0].enabled
    assert ig[0].token_encrypted == ""
    assert encrypted_credential(ig[0]) == page.token_encrypted
    assert page.enabled and credential_secrets.decrypt(page.token_encrypted) == "private-page-token"
    ig[0].enabled = True
    assert provider_view("instagram", [page, ig[0]])["capabilities"] == []


def test_page_without_link_reports_missing_scope_ambiguity(page_case, monkeypatch):
    db, page = page_case
    mock_page(monkeypatch, 200, {"id": "123", "name": "Page"})
    result = meta_auth.linked_instagram_check(db, page)
    assert result["http_status"] == 200
    assert result["capability"] == "PERMISSION_MISSING_OR_LINK_UNAVAILABLE"
    assert not result["instagram_business_account_exists"]
    assert db.scalars(select(PlatformAccount).where(PlatformAccount.platform == "instagram")).all() == []


def test_page_without_link_with_identity_scope_reports_no_api_link(page_case, monkeypatch):
    db, page = page_case
    mock_page(monkeypatch, 200, {"id": "123", "name": "Page"})
    monkeypatch.setattr(meta_auth, "token_metadata", lambda token, **kwargs: {
        "valid": True, "scopes": ["instagram_basic"], "token_type": "PAGE",
        "expires_at": None, "data_access_expires_at": None})
    assert meta_auth.linked_instagram_check(db, page)["capability"] == "NO_API_LINK"


def test_permission_error_is_structured_and_redacted(page_case, monkeypatch, caplog):
    db, page = page_case
    mock_page(monkeypatch, 403, {"error": {"type": "OAuthException", "code": 10, "error_subcode": 2018001,
                                      "message": "Missing instagram_basic for private-page-token app-secret"}})
    with caplog.at_level(logging.INFO):
        result = meta_auth.linked_instagram_check(db, page)
    assert result["http_status"] == 403 and result["error_code"] == 10
    assert result["error_subcode"] == 2018001 and result["capability"] == "ACCESS_DENIED"
    assert "instagram_basic" in result["error_message"]
    assert "private-page-token" not in str(result) + caplog.text
    assert "app-secret" not in str(result) + caplog.text


@pytest.mark.parametrize("body", [["unexpected"], {"id": "wrong", "name": "Page"},
                                   {"id": "123", "name": "Page", "instagram_business_account": []}])
def test_malformed_graph_response_does_not_register(page_case, monkeypatch, body):
    db, page = page_case
    mock_page(monkeypatch, 200, body)
    result = meta_auth.linked_instagram_check(db, page)
    assert result["capability"] == "MALFORMED_RESPONSE"
    assert db.scalars(select(PlatformAccount).where(PlatformAccount.platform == "instagram")).all() == []
