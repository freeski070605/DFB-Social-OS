import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db, utcnow
from app.knowledge.review import safety_reasons
from app.knowledge.service import search
from app.main import app
from app.models import Admin, AdminSession, Audit, Brand, Knowledge, SystemSetting


@pytest.fixture
def review_http():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Brand(id=1, name="Life Help", slug="life_help", config={"pillars": ["cleaning", "budgeting basics"]}),
                    Brand(id=2, name="Other", slug="other", config={"pillars": ["cleaning"]})])
        admin = Admin(username="reviewer", password_hash="unused")
        db.add(admin)
        db.flush()
        for raw, csrf in (("review-session-1", "csrf-one"), ("review-session-2", "csrf-two")):
            db.add(AdminSession(token_hash=hashlib.sha256(raw.encode()).hexdigest(), admin_id=admin.id,
                                csrf=csrf, expires_at=utcnow() + timedelta(hours=1)))
        db.commit()

        def test_db():
            yield db

        app.dependency_overrides[get_db] = test_db
        client = TestClient(app)
        client.cookies.set("dfb_session", "review-session-1")
        try:
            yield db, client
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


def item(brand_id, title, *, category="cleaning", source="DFB editorial guidance", tags=None,
         restrictions="", verification="PENDING", enabled=True):
    return Knowledge(brand_id=brand_id, title=title, category=category, body=f"Reference body for {title}.",
                     source=source, tags=tags or [], restrictions=restrictions,
                     verification=verification, enabled=enabled)


def post(client, brand_id, suffix, body, csrf="csrf-one"):
    return client.post(f"/api/brands/{brand_id}/knowledge/review/{suffix}", json=body,
                       headers={"X-CSRF-Token": csrf})


@pytest.mark.parametrize("category,source,tags,restrictions,expected", [
    ("basic cooking", "USDA", [], "", "Food safety"),
    ("cleaning", "DFB", ["bleach"], "", "Cleaning chemical safety"),
    ("general", "DFB", ["electrical wiring"], "", "Electrical safety"),
    ("general", "DFB", [], "Check for gas leaks", "Gas safety"),
    ("beginner home maintenance", "DFB", [], "", "Home repair safety"),
    ("budgeting basics", "CFPB", [], "", "Financial guidance"),
    ("career & work basics", "DFB", [], "", "Employment or legal"),
    ("general", "DFB", ["medical guidance"], "", "Medical or health"),
])
def test_deterministic_safety_classification(category, source, tags, restrictions, expected):
    assert expected in safety_reasons(category, source, tags, restrictions)


def test_filtered_selection_safety_approval_audit_and_retrieval(review_http):
    db, client = review_http
    db.add_all([item(1, "Editorial checklist"),
                item(1, "Budget guidance", category="budgeting basics", source="CFPB",
                     restrictions="General information only"),
                item(1, "Food storage", source="USDA", tags=["food safety"]),
                item(2, "Other brand checklist")])
    db.commit()
    path = "/api/brands/1/knowledge/review"
    filtered = client.get(path, params={"verification": "PENDING", "category": "budgeting basics",
                                        "source_contains": "CFPB"})
    assert filtered.status_code == 200
    assert (filtered.json()["total"], [row["title"] for row in filtered.json()["rows"]]) == (1, ["Budget guidance"])
    assert filtered.json()["rows"][0]["safety_reasons"]
    assert client.get(path, params={"source": "DFB editorial guidance"}).json()["total"] == 1
    assert client.get(path, params={"tag": "food safety", "safety": "sensitive"}).json()["total"] == 1
    visible = filtered.json()["rows"]
    selected_visible = post(client, 1, "selection", {"scope": "ids", "ids": [
        {"id": row["id"], "version": row["version"]} for row in visible]})
    assert selected_visible.status_code == 200 and selected_visible.json()["count"] == 1
    assert selected_visible.json()["sensitive_count"] == 1

    all_pending = post(client, 1, "selection", {"scope": "matching", "filters": {"verification": "PENDING"}})
    assert all_pending.status_code == 200 and all_pending.json()["count"] == 3
    assert all_pending.json()["restrictions_count"] == 1
    token = all_pending.json()["selection_token"]
    assert post(client, 1, "apply", {"selection_token": token, "action": "APPROVE"}, csrf="bad").status_code == 403
    approved = post(client, 1, "apply", {"selection_token": token, "action": "APPROVE"})
    assert approved.status_code == 200
    assert approved.json()["affected"] == 1 and approved.json()["skipped_sensitive"] == 2
    assert {row.title for row in search(db, 1, approved=True)} == {"Editorial checklist"}
    assert search(db, 2, approved=True) == []

    sensitive = post(client, 1, "selection", {"scope": "matching", "filters": {
        "verification": "PENDING", "safety": "sensitive"}})
    assert sensitive.status_code == 200 and sensitive.json()["count"] == 2
    included = post(client, 1, "apply", {"selection_token": sensitive.json()["selection_token"],
                                        "action": "APPROVE", "include_sensitive": True})
    assert included.status_code == 200 and included.json()["affected"] == 2
    assert {row.title for row in search(db, 1, approved=True)} == {
        "Editorial checklist", "Budget guidance", "Food storage"}
    audits = db.scalars(select(Audit).where(Audit.brand_id == 1, Audit.action == "knowledge.bulk_approve")
                        .order_by(Audit.id)).all()
    assert len(audits) == 2
    assert audits[0].actor == "reviewer" and audits[0].details["number_affected"] == 1
    assert len(audits[1].details["record_ids"]) == 2 and audits[1].created_at is not None
    assert "Reference body" not in str(audits[1].details)


def test_visible_page_selection_actions_and_brand_isolation(review_http):
    db, client = review_http
    db.add_all([item(1, f"Checklist {number:02}") for number in range(35)] + [item(2, "Other brand")])
    db.commit()
    first = client.get("/api/brands/1/knowledge/review", params={"verification": "PENDING", "sort": "title"}).json()
    assert first["total"] == 35 and len(first["rows"]) == 30
    visible_ids = [{"id": row["id"], "version": row["version"]} for row in first["rows"]]
    chosen = post(client, 1, "selection", {"scope": "ids", "ids": visible_ids})
    assert chosen.status_code == 200 and chosen.json()["count"] == 30
    disabled = post(client, 1, "apply", {"selection_token": chosen.json()["selection_token"], "action": "DISABLE"})
    assert disabled.status_code == 200 and disabled.json()["affected"] == 30
    assert client.get("/api/brands/1/knowledge/review", params={"enabled": "true"}).json()["total"] == 5
    disabled_selection = post(client, 1, "selection", {"scope": "matching", "filters": {"enabled": False}})
    enabled = post(client, 1, "apply", {"selection_token": disabled_selection.json()["selection_token"],
                                        "action": "ENABLE"})
    assert enabled.status_code == 200 and enabled.json()["affected"] == 30
    assert client.get("/api/brands/1/knowledge/review", params={"enabled": "false"}).json()["total"] == 0
    assert client.get("/api/brands/2/knowledge/review").json()["total"] == 1
    other = db.scalar(select(Knowledge).where(Knowledge.brand_id == 2))
    assert post(client, 1, "selection", {"scope": "ids", "ids": [{"id": other.id,
        "version": "0" * 64}]}).status_code == 409


def test_stale_session_brand_expiry_tamper_and_verification_actions(review_http):
    db, client = review_http
    row = item(1, "Review me")
    db.add(row)
    db.commit()
    visible = client.get("/api/brands/1/knowledge/review").json()["rows"][0]
    body = {"scope": "ids", "ids": [{"id": visible["id"], "version": visible["version"]}]}
    selection = post(client, 1, "selection", body).json()
    token = selection["selection_token"]
    assert post(client, 2, "apply", {"selection_token": token, "action": "APPROVE"}).status_code == 403
    client.cookies.set("dfb_session", "review-session-2")
    assert post(client, 1, "apply", {"selection_token": token, "action": "APPROVE"},
                csrf="csrf-two").status_code == 403
    client.cookies.set("dfb_session", "review-session-1")
    assert post(client, 1, "apply", {"selection_token": token + "x", "action": "APPROVE"}).status_code == 409
    row.body = "Materially changed after selection"
    db.commit()
    assert post(client, 1, "apply", {"selection_token": token, "action": "APPROVE"}).status_code == 409
    assert row.verification == "PENDING"
    refreshed = client.get("/api/brands/1/knowledge/review").json()["rows"][0]
    assert post(client, 1, "selection", body).status_code == 409
    next_selection = post(client, 1, "selection", {"scope": "ids", "ids": [{"id": row.id,
        "version": refreshed["version"]}]}).json()
    state = db.get(SystemSetting, "knowledge_review:" + hashlib.sha256(
        next_selection["selection_token"].encode()).hexdigest())
    state.value = {**state.value, "expires_at": 0}
    db.commit()
    assert post(client, 1, "apply", {"selection_token": next_selection["selection_token"],
                                    "action": "APPROVE"}).status_code == 409
    for action, expected in (("REJECT", "REJECTED"), ("PENDING", "PENDING"), ("APPROVE", "APPROVED")):
        current = client.get("/api/brands/1/knowledge/review").json()["rows"][0]
        chosen = post(client, 1, "selection", {"scope": "ids", "ids": [{"id": row.id,
            "version": current["version"]}]}).json()
        applied = post(client, 1, "apply", {"selection_token": chosen["selection_token"], "action": action})
        assert applied.status_code == 200 and applied.json()["affected"] == 1
        db.refresh(row)
        assert row.verification == expected
        assert post(client, 1, "apply", {"selection_token": chosen["selection_token"],
                                        "action": action}).status_code == 409
    assert {entry.id for entry in search(db, 1, approved=True)} == {row.id}


def test_thousands_are_paged_and_applied_in_one_transaction(review_http, monkeypatch):
    db, client = review_http
    db.add_all([item(1, f"Large review {number}") for number in range(2500)])
    db.commit()
    listed = client.get("/api/brands/1/knowledge/review").json()
    assert listed["total"] == 2500 and len(listed["rows"]) == 30
    selection = post(client, 1, "selection", {"scope": "matching", "filters": {
        "verification": "PENDING", "source": "DFB editorial guidance"}})
    assert selection.status_code == 200 and selection.json()["count"] == 2500
    commits = 0
    original_commit = db.commit

    def counted_commit():
        nonlocal commits
        commits += 1
        return original_commit()

    monkeypatch.setattr(db, "commit", counted_commit)
    result = post(client, 1, "apply", {"selection_token": selection.json()["selection_token"],
                                        "action": "DISABLE"})
    assert result.status_code == 200 and result.json()["affected"] == 2500
    assert commits == 1
    assert client.get("/api/brands/1/knowledge/review", params={"enabled": "false"}).json()["total"] == 2500
