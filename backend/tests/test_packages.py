from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
import pytest

from app.core.errors import DomainError
from app.db.session import Base
from app.models import Brand, Content, ContentDerivative, Knowledge
from app.packages import service
from app.packages.schemas import PackageInput, VideoPlan
from app.accounts.capabilities import provider_view
from app.models import PlatformAccount
from app.api import youtube_auth
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Brand(id=1, name="LIFE, APPARENTLY.", slug="life_help",
            config={"pillars": ["routines & weekly resets"]}),
            Brand(id=2, name="Other", slug="other", config={"pillars": ["routines & weekly resets"]})])
        session.add_all([Knowledge(id=1, brand_id=1, category="routines & weekly resets", title="Evening routine",
            body="Set out tomorrow's essentials tonight", source="source", verification="APPROVED", enabled=True),
            Knowledge(id=2, brand_id=2, category="routines & weekly resets", title="Other",
            body="Other brand", verification="APPROVED", enabled=True)])
        session.add(Content(id=12, brand_id=1, topic="Tomorrow", pillar="routines & weekly resets",
            knowledge_refs=[1], revision=2, status="REVIEW"))
        session.commit()
        yield session
    engine.dispose()


def test_attach_preserves_content_and_is_idempotent(db):
    before = db.get(Content, 12)
    snapshot = (before.id, before.revision, before.status, before.knowledge_refs)
    first = service.attach_content(db, 1, 12, "test")
    second = service.attach_content(db, 1, 12, "test")
    assert first.id == second.id
    assert (before.id, before.revision, before.status, before.knowledge_refs) == snapshot
    derivative = db.scalar(select(ContentDerivative).where(ContentDerivative.package_id == first.id))
    assert derivative.content_id == 12 and derivative.used_knowledge_refs == [1]


def test_cross_brand_grounding_fails_closed(db):
    with pytest.raises(DomainError):
        service.create(db, 1, PackageInput(topic="Test", pillar="routines & weekly resets",
                                            knowledge_refs=[2]), "test")


def test_video_schema_and_actual_used_provenance(db):
    payload = {"title": "Prepare tomorrow", "hook": "Tomorrow starts tonight", "target_duration_seconds": 45,
        "narration": "One useful thing to do tonight is set out tomorrow's essentials. " * 5,
        "scenes": [{"sequence": 1, "narration": "Set it out", "visual_type": "demonstration",
                    "visual_direction": "Show the bag", "estimated_seconds": 20, "knowledge_refs": [1]},
                   {"sequence": 2, "narration": "Use it", "visual_type": "original_footage",
                    "visual_direction": "Morning scene", "estimated_seconds": 25, "knowledge_refs": [1]}],
        "knowledge_refs": [1]}
    plan = VideoPlan.model_validate(payload)
    assert service.evaluate_output(plan, "YOUTUBE_SHORT", {1})["issues"] == []
    with pytest.raises(DomainError):
        service.evaluate_output(plan, "YOUTUBE_SHORT", {2})
    payload["scenes"][0]["knowledge_refs"] = [2]
    with pytest.raises(DomainError):
        service.evaluate_output(VideoPlan.model_validate(payload), "YOUTUBE_SHORT", {1})


def test_capabilities_require_one_verified_selected_account():
    first = PlatformAccount(brand_id=1, platform="facebook", account_id="1", token_encrypted="secret",
                            enabled=False, config={"token_status": "healthy"})
    second = PlatformAccount(brand_id=1, platform="facebook", account_id="2", token_encrypted="secret",
                             enabled=False, config={"token_status": "healthy"})
    assert provider_view("facebook", [first, second])["capabilities"] == []
    first.enabled = True
    assert "CAN_PUBLISH_TEXT" not in provider_view("facebook", [first, second])["capabilities"]
    first.config = {"token_status": "healthy", "permissions": ["pages_manage_posts"]}
    assert "CAN_PUBLISH_TEXT" in provider_view("facebook", [first, second])["capabilities"]
    assert provider_view("tiktok", [])["support"] == "PLANNED"
    assert provider_view("youtube", [])["capabilities"] == []


def test_production_export_is_structured_and_brand_scoped(db):
    package = service.attach_content(db, 1, 12, "test")
    db.add(ContentDerivative(id=8, package_id=package.id, brand_id=1, platform="youtube",
        format="YOUTUBE_SHORT", status="REVIEW", output={"title": "Tomorrow", "hook": "Start here",
        "target_duration_seconds": 30, "narration": "Prepare one useful thing tonight",
        "scenes": [{"sequence": 1, "visual_type": "demonstration", "estimated_seconds": 15,
                    "knowledge_refs": [1]},
                   {"sequence": 2, "visual_type": "original_footage", "estimated_seconds": 15,
                    "knowledge_refs": [1]}], "knowledge_refs": [1]}, used_knowledge_refs=[1]))
    db.flush()
    payload = service.production_export(db, 1, 8)
    assert payload["provider"] == "manual_export"
    assert payload["aspect_ratio"] == "9:16" and payload["provenance"][0]["id"] == 1
    assert "password" not in str(payload).lower()
    with pytest.raises(DomainError):
        service.production_export(db, 2, 8)


def test_youtube_oauth_requests_only_readonly_and_discovers_unselected(db, monkeypatch):
    cfg = SimpleNamespace(youtube_client_id="client", youtube_client_secret="secret",
        youtube_redirect_uri="https://example.test/api/youtube/callback", encryption_key="key")
    monkeypatch.setattr(youtube_auth, "configured", lambda: cfg)
    monkeypatch.setattr(youtube_auth, "encrypt", lambda value: "encrypted:" + value)
    monkeypatch.setattr(youtube_auth, "channels", lambda token: [
        {"id": "channel-1", "snippet": {"title": "Owner channel"}},
        {"id": "channel-2", "snippet": {"title": "Second channel"}}])

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

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(youtube_auth.httpx, "Client", Client)
    admin = SimpleNamespace(id=1, username="admin")
    request = SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(token_hash="session")))
    url = youtube_auth.start(1, request, admin=admin, db=db)["authorization_url"]
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [youtube_auth.SCOPE]
    response = youtube_auth.callback(request, code="code", state=query["state"][0], admin=admin, db=db)
    assert response.status_code == 303
    discovered = db.scalars(select(PlatformAccount).where(PlatformAccount.platform == "youtube")).all()
    assert len(discovered) == 2 and all(not row.enabled for row in discovered)
