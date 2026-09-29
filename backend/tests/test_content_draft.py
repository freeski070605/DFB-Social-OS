import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.content import service as content_service
from app.db.session import Base, get_db, utcnow
from app.main import app
from app.models import Admin, AdminSession, Brand, Content, Job, Knowledge
from app.schemas.domain import GenerationInput


@pytest.fixture
def content_http():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Brand(id=1, name="Life Help", slug="life_help", config={"pillars": [
            "grocery & food savings", "routines & weekly resets"]}),
                    Brand(id=2, name="Other", slug="other", config={"pillars": ["routines & weekly resets"]})])
        admin = Admin(username="editor", password_hash="unused")
        db.add(admin)
        db.flush()
        raw_token, csrf = "content-test-session", "content-test-csrf"
        db.add(AdminSession(token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), admin_id=admin.id,
                            csrf=csrf, expires_at=utcnow() + timedelta(hours=1)))
        db.commit()

        def test_db():
            yield db

        app.dependency_overrides[get_db] = test_db
        client = TestClient(app)
        client.cookies.set("dfb_session", raw_token)
        try:
            yield db, client, csrf
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


def draft_payload(**changes):
    return {"topic": "7 things to do tonight to make tomorrow easier",
            "pillar": "routines & weekly resets", "format": "carousel",
            "hook": "Tomorrow gets easier when you stop leaving everything for tomorrow.",
            "body": "", "slides": [], "caption": "", "cta": "", "hashtags": [],
            "knowledge_refs": [], "sources": [], "targets": ["manual"], "parent_id": None} | changes


def knowledge(brand_id, title, category, verification="APPROVED", enabled=True):
    return Knowledge(brand_id=brand_id, title=title, category=category, body=f"Guidance for {title}",
                     source="DFB editorial guidance", restrictions="General information only", tags=[],
                     verification=verification, enabled=enabled)


def post(client, path, payload, csrf):
    return client.post(path, json=payload, headers={"X-CSRF-Token": csrf})


def test_incomplete_draft_saves_reopens_and_cannot_advance(content_http):
    db, client, csrf = content_http
    path = "/api/brands/1/content"
    assert client.post(path, json=draft_payload()).status_code == 403
    response = post(client, path, draft_payload(), csrf)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["status"] == "DRAFT" and saved["slides"] == []
    assert saved["quality"]["status"] == "BLOCKED"
    reopened = client.get(f"{path}/{saved['id']}")
    assert reopened.status_code == 200
    assert all(reopened.json()[key] == draft_payload()[key] for key in
               ("topic", "pillar", "format", "hook", "body", "caption", "slides"))
    assert post(client, f"{path}/{saved['id']}/transition", {"state": "REVIEW"}, csrf).status_code == 400
    assert db.scalar(select(Content).where(Content.id == saved["id"])).status == "DRAFT"
    assert db.scalar(select(func.count()).select_from(Job)) == 0  # Saving never queues Ollama.


def test_original_blank_cover_request_is_valid_only_as_draft(content_http):
    _, client, csrf = content_http
    original_slide = [{"title": "", "body": "", "kind": "cover", "items": []}]
    response = post(client, "/api/brands/1/content", draft_payload(slides=original_slide), csrf)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DRAFT"
    assert "Slide 1 title is required" in response.json()["quality"]["errors"]
    transition = post(client, f"/api/brands/1/content/{response.json()['id']}/transition",
                      {"state": "REVIEW"}, csrf)
    assert transition.status_code == 400


def test_partial_slide_is_draft_only_and_render_requires_title(content_http):
    db, client, csrf = content_http
    partial = [{"title": "", "body": "A partial explanation", "kind": "cover", "items": []}]
    response = post(client, "/api/brands/1/content", draft_payload(slides=partial), csrf)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["slides"] == partial
    assert "Slide 1 title is required" in saved["quality"]["errors"]
    transition = post(client, f"/api/brands/1/content/{saved['id']}/transition", {"state": "REVIEW"}, csrf)
    assert transition.status_code == 400 and "Slide 1 title is required" in transition.json()["detail"]
    rendered = post(client, f"/api/brands/1/content/{saved['id']}/render", {}, csrf)
    assert rendered.status_code == 400 and "Slide 1 title is required" in rendered.json()["detail"]
    put = client.put(f"/api/brands/1/content/{saved['id']}", json=draft_payload(slides=[
        {"title": "A complete cover", "body": "A useful explanation", "kind": "cover", "items": []}]),
        headers={"X-CSRF-Token": csrf})
    assert put.status_code == 200
    assert post(client, f"/api/brands/1/content/{saved['id']}/transition", {"state": "REVIEW"}, csrf).status_code == 200


def test_invalid_pillar_and_other_brand_knowledge_are_rejected(content_http):
    db, client, csrf = content_http
    other = knowledge(2, "Other brand fact", "routines & weekly resets")
    db.add(other)
    db.commit()
    invalid = post(client, "/api/brands/1/content", draft_payload(pillar="unknown pillar"), csrf)
    assert invalid.status_code == 400 and "configured pillar" in invalid.json()["detail"]
    attached = post(client, "/api/brands/1/content", draft_payload(knowledge_refs=[other.id]), csrf)
    assert attached.status_code == 404
    assert db.scalar(select(func.count()).select_from(Content)) == 0
    canonical = post(client, "/api/brands/1/content", draft_payload(pillar=" ROUTINES  & WEEKLY RESETS "), csrf)
    assert canonical.status_code == 200 and canonical.json()["pillar"] == "routines & weekly resets"


def test_candidates_change_with_pillar_and_generation_revalidates(content_http, monkeypatch):
    db, client, csrf = content_http
    grocery = knowledge(1, "Inventory before shopping", "grocery & food savings")
    routine = knowledge(1, "Evening reset", "routines & weekly resets")
    pending = knowledge(1, "Unreviewed routine", "routines & weekly resets", "PENDING")
    disabled = knowledge(1, "Disabled routine", "routines & weekly resets", enabled=False)
    other = knowledge(2, "Other brand routine", "routines & weekly resets")
    db.add_all([grocery, routine, pending, disabled, other])
    db.commit()
    path = "/api/brands/1/knowledge/candidates"
    grocery_rows = client.get(path, params={"topic": "tomorrow easier", "pillar": "grocery & food savings"}).json()
    routine_rows = client.get(path, params={"topic": "tomorrow easier", "pillar": "routines & weekly resets"}).json()
    assert [row["id"] for row in grocery_rows] == [grocery.id]
    assert [row["id"] for row in routine_rows] == [routine.id]
    generate_path = "/api/brands/1/generate"
    invalid = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                            "format": "carousel", "knowledge_refs": [grocery.id]}, csrf)
    assert invalid.status_code == 400
    foreign = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                            "format": "carousel", "knowledge_refs": [other.id]}, csrf)
    assert foreign.status_code == 400
    queued = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                           "format": "carousel", "knowledge_refs": [routine.id]}, csrf)
    assert queued.status_code == 200 and queued.json()["payload"]["knowledge_refs"] == [routine.id]

    class CapturedInference(Exception):
        pass

    captured = {}

    class FakeProvider:
        def generate(self, instruction, context, schema):
            captured["ids"] = [row["id"] for row in context["knowledge"]]
            raise CapturedInference

    monkeypatch.setattr(content_service, "provider", lambda _: FakeProvider())
    with pytest.raises(CapturedInference):
        content_service.generate(db, 1, GenerationInput(topic="tomorrow easier", pillar="routines & weekly resets",
                                                       knowledge_refs=[routine.id]))
    assert captured["ids"] == [routine.id]
    routine.enabled = False
    db.commit()
    with pytest.raises(Exception, match="approved, enabled"):
        content_service.generate(db, 1, GenerationInput(topic="tomorrow easier", pillar="routines & weekly resets",
                                                       knowledge_refs=[routine.id]))
    assert captured["ids"] == [routine.id]  # No second inference call.
