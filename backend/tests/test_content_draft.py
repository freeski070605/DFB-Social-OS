import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.content import service as content_service
from app.scheduling.worker import execute
from app.core.errors import DomainError, ProviderError
from app.db.session import Base, get_db, utcnow
from app.main import app
from app.models import Admin, AdminSession, Brand, Content, Job, Knowledge
from app.schemas.domain import EditorialPlan, GeneratedContent, GenerationInput


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
    assert saved["quality"]["status"] == "NOT_SCORED" and saved["quality"]["score"] is None
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


def test_cover_title_only_is_not_a_quality_score_or_reviewable(content_http):
    _, client, csrf = content_http
    cover = [{"title": "7 things to do tonight", "body": "", "kind": "cover", "items": []}]
    response = post(client, "/api/brands/1/content", draft_payload(slides=cover), csrf)
    assert response.status_code == 200
    saved = response.json()
    assert saved["quality"]["score"] is None and saved["quality"]["status"] == "NOT_SCORED"
    assert any("slide explanation/items" in error for error in saved["quality"]["errors"])
    assert post(client, f"/api/brands/1/content/{saved['id']}/transition",
                {"state": "REVIEW"}, csrf).status_code == 400


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
    assert all(row["brand_id"] == 1 and row["category"] == "routines & weekly resets" and
               row["verification"] == "APPROVED" and row["enabled"] for row in routine_rows)
    generate_path = "/api/brands/1/generate"
    ungrounded = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                             "format": "carousel", "knowledge_refs": []}, csrf)
    assert ungrounded.status_code == 400 and "Select and attach" in ungrounded.json()["detail"]
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    invalid = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                            "format": "carousel", "knowledge_refs": [grocery.id]}, csrf)
    assert invalid.status_code == 400
    foreign = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                            "format": "carousel", "knowledge_refs": [other.id]}, csrf)
    assert foreign.status_code == 400
    unapproved = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                              "format": "carousel", "knowledge_refs": [pending.id]}, csrf)
    assert unapproved.status_code == 400
    stale_attachment = post(client, "/api/brands/1/content", draft_payload(knowledge_refs=[grocery.id]), csrf)
    assert stale_attachment.status_code == 400
    attached = post(client, "/api/brands/1/content", draft_payload(knowledge_refs=[routine.id]), csrf)
    assert attached.status_code == 200 and attached.json()["knowledge_refs"] == [routine.id]
    assert not any("No approved knowledge attached" in warning for warning in attached.json()["quality"]["warnings"])
    queued = post(client, generate_path, {"topic": "tomorrow easier", "pillar": "routines & weekly resets",
                                           "format": "carousel", "knowledge_refs": [routine.id]}, csrf)
    assert queued.status_code == 200 and queued.json()["payload"]["knowledge_refs"] == [routine.id]

    class CapturedInference(Exception):
        pass

    captured = {}

    class FakeProvider:
        def generate(self, instruction, context, schema):
            captured["ids"] = [row["id"] for row in context["knowledge"]]
            captured["topic"] = context["topic"]
            captured["pillar"] = context["pillar"]
            raise CapturedInference

    monkeypatch.setattr(content_service, "provider", lambda _: FakeProvider())
    with pytest.raises(CapturedInference):
        content_service.generate(db, 1, GenerationInput(topic="tomorrow easier", pillar="routines & weekly resets",
                                                       knowledge_refs=[routine.id]))
    assert captured["ids"] == [routine.id]
    assert captured["topic"] == "tomorrow easier"
    assert captured["pillar"] == "routines & weekly resets"
    routine.enabled = False
    db.commit()
    with pytest.raises(Exception, match="approved, enabled"):
        content_service.generate(db, 1, GenerationInput(topic="tomorrow easier", pillar="routines & weekly resets",
                                                       knowledge_refs=[routine.id]))
    assert captured["ids"] == [routine.id]  # No second inference call.


def test_current_topic_ranks_approved_routine_records_first(content_http):
    db, client, _ = content_http
    db.add_all([knowledge(1, "Inventory before shopping", "grocery & food savings"),
                knowledge(1, "Use a nightly closing routine", "routines & weekly resets"),
                knowledge(1, "Prepare for Monday the night before", "routines & weekly resets"),
                knowledge(1, "Review tomorrow before ending today", "routines & weekly resets"),
                knowledge(1, "Prepare one thing for future-you", "routines & weekly resets")])
    db.commit()
    response = client.get("/api/brands/1/knowledge/candidates", params={
        "topic": "7 things to do tonight to make tomorrow easier", "pillar": "routines & weekly resets"})
    assert response.status_code == 200
    titles = [row["title"] for row in response.json()]
    assert "Inventory before shopping" not in titles
    assert titles[0] == "Use a nightly closing routine"
    assert "Review tomorrow before ending today" in titles


def test_editor_generation_job_updates_current_draft_atomically(content_http, monkeypatch):
    db, client, csrf = content_http
    selected = [knowledge(1, "Prepare one thing for future-you", "routines & weekly resets"),
                knowledge(1, "Review tomorrow before ending today", "routines & weekly resets")]
    grocery = knowledge(1, "Inventory before shopping", "grocery & food savings")
    pending = knowledge(1, "Unverified routine", "routines & weekly resets", "PENDING")
    db.add_all([*selected, grocery, pending])
    db.commit()
    draft = post(client, "/api/brands/1/content", draft_payload(slides=[
        {"title": "7 things to do tonight to make tomorrow easier", "body": "", "kind": "cover", "items": []}]), csrf).json()
    request = {"topic": draft["topic"], "pillar": draft["pillar"], "format": draft["format"],
               "parent_id": draft["id"], "knowledge_refs": [row.id for row in selected], "update_current": True}
    assert client.post("/api/brands/1/generate", json=request).status_code == 403
    queued = post(client, "/api/brands/1/generate", request, csrf)
    assert queued.status_code == 200
    job_id = queued.json()["id"]
    assert client.get(f"/api/brands/1/jobs/{job_id}").status_code == 200
    assert db.get(Content, draft["id"]).revision == 1
    captured = {}
    titles = ["Put your bag by the door", "Review tomorrow's calendar", "Choose tomorrow's first task",
              "Lay out clothes tonight", "Check tomorrow's weather", "Put keys in one place",
              "Clear the breakfast space"]
    explanations = ["Put the bag and anything you need beside the door before bed for tomorrow morning.",
                    "Look at tomorrow's appointments so an early start is not a surprise.",
                    "Pick the first task now so you do not decide under morning pressure.",
                    "Choose the clothes you need and leave them where you can find them tomorrow morning.",
                    "Check the forecast tonight and set out any rain gear you need.",
                    "Leave your keys in their usual place before sleep so you can find them tomorrow morning.",
                    "Clear one spot tonight so breakfast has room tomorrow."]

    class FakeProvider:
        def generate(self, instruction, context, schema):
            if schema is EditorialPlan:
                captured["plan_context"] = context
                return EditorialPlan.model_validate({
                    "audience_promise": "Seven practical tonight actions that make tomorrow easier",
                    "content_goal": "Help the reader prepare for the next day",
                    "angle": "Small decisions before bed", "candidate_points": titles,
                    "selected_points": [{"headline": title, "action": body, "support_ids": [selected[i % 2].id]}
                                        for i, (title, body) in enumerate(zip(titles, explanations))],
                    "rejected_points": [], "desired_slide_count": 8,
                    "recommended_layout_sequence": ["cover", "numbered_action", "numbered_action", "checklist",
                                                    "numbered_action", "statement", "numbered_action", "end"],
                    "caption_direction": "Add a short useful frame and natural CTA"})
            captured["write_context"] = context
            return GeneratedContent.model_validate({
                **draft_payload(), "hook": "Model hook that must not replace the saved hook",
                "slides": [{"title": "Cover", "body": "Seven small decisions tonight.", "kind": "cover", "items": []}] +
                          [{"title": title, "body": body, "kind": "numbered_action", "items": []}
                           for title, body in zip(titles, explanations)],
                "caption": "Rough mornings often begin with decisions left for morning. Pick one small reset tonight, and keep this list for an evening when you need a simpler start.",
                "knowledge_refs": [row.id for row in selected]})

    monkeypatch.setattr(content_service, "provider", lambda _: FakeProvider())
    execute(db, db.get(Job, job_id))
    db.commit()
    saved = client.get(f"/api/brands/1/content/{draft['id']}").json()
    assert saved["id"] == draft["id"] and saved["revision"] == 2 and saved["status"] == "REVIEW"
    assert saved["hook"] == draft["hook"] and len(saved["slides"]) == 8
    assert saved["knowledge_refs"] == [row.id for row in selected]
    assert [row["id"] for row in saved["generation"]["knowledge"]] == [row.id for row in selected]
    assert db.get(Job, job_id).status == "DONE" and db.get(Job, job_id).target_id == draft["id"]
    assert db.scalar(select(func.count()).select_from(Content)) == 1
    assert captured["write_context"]["request"]["topic"] == draft["topic"]
    assert captured["write_context"]["request"]["pillar"] == draft["pillar"]
    assert [row["id"] for row in captured["write_context"]["knowledge"]] == [row.id for row in selected]
    assert grocery.id not in [row["id"] for row in captured["plan_context"]["knowledge"]]
    assert pending.id not in [row["id"] for row in captured["plan_context"]["knowledge"]]


def test_failed_or_stale_generation_preserves_draft(content_http, monkeypatch):
    db, client, csrf = content_http
    routine = knowledge(1, "Review tomorrow before ending today", "routines & weekly resets")
    db.add(routine)
    db.commit()
    draft = post(client, "/api/brands/1/content", draft_payload(), csrf).json()
    request = {"topic": draft["topic"], "pillar": draft["pillar"], "format": draft["format"],
               "parent_id": draft["id"], "knowledge_refs": [routine.id], "update_current": True}
    job_id = post(client, "/api/brands/1/generate", request, csrf).json()["id"]

    class InvalidProvider:
        def generate(self, *_):
            raise ProviderError("Local model returned invalid structured output")

    monkeypatch.setattr(content_service, "provider", lambda _: InvalidProvider())
    with pytest.raises(ProviderError, match="invalid structured output"):
        execute(db, db.get(Job, job_id))
    db.rollback()
    unchanged = db.get(Content, draft["id"])
    assert unchanged.revision == 1 and unchanged.body == "" and unchanged.status == "DRAFT"
    assert db.scalar(select(func.count()).select_from(Content)) == 1
    unchanged.revision = 2
    db.commit()
    with pytest.raises(DomainError, match="Draft changed"):
        execute(db, db.get(Job, job_id))
    db.rollback()


def test_variant_preserves_original_uses_only_planned_provenance_and_revises_once(content_http, monkeypatch):
    db, client, csrf = content_http
    direct = knowledge(1, "Prepare one thing for future-you", "routines & weekly resets")
    adjacent = knowledge(1, "Use a morning launch routine", "routines & weekly resets")
    db.add_all([direct, adjacent])
    db.commit()
    payload = draft_payload(topic="2 things to do tonight to make tomorrow easier",
                            hook="Two small decisions tonight can ease tomorrow.")
    parent = post(client, "/api/brands/1/content", payload, csrf).json()
    calls = []

    class FakeProvider:
        def generate(self, instruction, context, schema):
            calls.append(schema.__name__)
            if schema is EditorialPlan:
                assert {row["id"] for row in context["knowledge"]} == {direct.id}
                assert adjacent.title in context["adjacent_titles_to_avoid"]
                return EditorialPlan.model_validate({
                    "audience_promise": "Two actions tonight that ease tomorrow morning",
                    "content_goal": "Remove two avoidable morning decisions", "angle": "Small evening preparation",
                    "candidate_points": ["Pack tomorrow's bag", "Put keys by the door", "Use a morning routine"],
                    "selected_points": [
                        {"headline": "Pack tomorrow's bag", "action": "Put the bag and essentials together before bed for tomorrow morning.",
                         "support_ids": [direct.id]},
                        {"headline": "Put keys by the door", "action": "Leave keys beside your bag tonight.",
                         "support_ids": [direct.id]}],
                    "rejected_points": [{"point": "Use a morning routine", "reason": "off_topic"}],
                    "desired_slide_count": 3,
                    "recommended_layout_sequence": ["cover", "numbered_action", "end"],
                    "caption_direction": "Explain why two small evening choices help"})
            assert [row["id"] for row in context["knowledge"]] == [direct.id]
            caption = ("Start your day off right. #Morning" if calls.count("GeneratedContent") == 1 else
                       "Two small decisions tonight leave less to sort out before breakfast tomorrow. Pick the one that would clear the most friction from your morning.")
            return GeneratedContent.model_validate({
                **payload, "slides": [
                    {"title": "2 things to do tonight", "body": "A short reset for tomorrow.", "kind": "cover", "items": []},
                    {"title": "Pack tomorrow's bag", "body": "Packing your bag tonight will reduce stress tomorrow.",
                     "kind": "numbered_action", "items": []},
                    {"title": "Put keys by the door", "body": "Leave your keys in one known place beside the bag tonight for tomorrow morning.",
                     "kind": "end", "items": []}],
                "caption": caption, "knowledge_refs": [direct.id]})

    monkeypatch.setattr(content_service, "provider", lambda _: FakeProvider())
    variant = content_service.generate(db, 1, GenerationInput(topic=payload["topic"], pillar=payload["pillar"],
                                        format="carousel", parent_id=parent["id"],
                                        knowledge_refs=[direct.id, adjacent.id]))
    db.commit()
    assert calls == ["EditorialPlan", "GeneratedContent", "GeneratedContent"]
    assert variant.parent_id == parent["id"] and variant.id != parent["id"]
    assert variant.generation["ai_revised"] is True
    assert variant.knowledge_refs == [direct.id]
    assert [row["id"] for row in variant.generation["knowledge"]] == [direct.id]
    assert len(variant.slides) == 3 and variant.slides[1]["kind"] == "numbered_action"
    assert variant.slides[1]["body"] == "Put the bag and essentials together before bed for tomorrow morning."
    original = db.get(Content, parent["id"])
    assert original.revision == 1 and original.status == "DRAFT" and original.slides == []
