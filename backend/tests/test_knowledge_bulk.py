import csv
import io
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.errors import DomainError
from app.db.session import Base
from app.knowledge import bulk
from app.knowledge.service import search
from app.models import Audit, Brand, Knowledge


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Brand(id=1, name="Life Help", slug="life_help", config={"pillars": ["Health", "Finance"]}),
                         Brand(id=2, name="ReemTeam", slug="reemteam", config={"pillars": ["Health", "Finance"]})])
        session.commit()
        yield session
    engine.dispose()


def csv_content(*rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=bulk.COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def entry(**changes):
    return {"title": "A useful fact", "category": "Health", "body": "Detailed and sourced guidance.",
            "source": "  Journal of Examples, 2025  ", "usage_restrictions": "Do not give personal advice.",
            "tags": "health, practical tips", "verification": "APPROVED", "available_for_retrieval": "true"} | changes


def preview(db, brand_id, content, file_format):
    return bulk.preview(db, brand_id, content, file_format, "admin", 10, "session")


def commit(db, brand_id, content, file_format, token, mode="SKIP_DUPLICATES", skip_invalid=False):
    return bulk.commit(db, brand_id, content, file_format, token, mode, skip_invalid, "admin", 10, "session")


def test_csv_template_valid_import_and_retrieval_rules(db):
    assert next(csv.reader(io.StringIO(bulk.csv_template().lstrip("\ufeff")))) == list(bulk.COLUMNS)
    content = csv_content(entry())
    result = preview(db, 1, content, "csv")
    assert (result["total"], result["valid"], result["invalid"], result["duplicates"]) == (1, 1, 0, 0)
    assert db.scalars(select(Knowledge)).all() == []  # Preview does not create knowledge.
    summary = commit(db, 1, content, "csv", result["preview_token"])
    assert summary == {"total": 1, "created": 1, "updated": 0, "skipped": 0, "rejected": 0}
    item = db.scalar(select(Knowledge))
    assert item.source == "  Journal of Examples, 2025  "
    assert item.restrictions == "Do not give personal advice."
    assert item.tags == ["health", "practical tips"]
    assert [row.id for row in search(db, 1, approved=True)] == [item.id]
    assert search(db, 2, approved=True) == []
    audits = db.scalars(select(Audit).where(Audit.brand_id == 1).order_by(Audit.id)).all()
    assert [audit.action for audit in audits] == ["knowledge.import_started", "knowledge.import_previewed", "knowledge.import_committed"]
    assert audits[-1].details["created"] == 1
    assert all("Detailed and sourced guidance" not in json.dumps(audit.details) for audit in audits)


def test_json_import_pending_fallback_and_explicit_approval(db):
    rows = [entry(title="Unverified", verification="uncertain", tags=["one", " two "] , available_for_retrieval=True),
            entry(title="Approved", body="A distinct, approved reference.", verification="APPROVED", tags=["verified"], available_for_retrieval=True)]
    content = json.dumps(rows)
    result = preview(db, 1, content, "json")
    assert result["valid"] == 2
    assert result["rows"][0]["record"]["verification"] == "PENDING"
    assert result["warnings"] >= 1
    commit(db, 1, content, "json", result["preview_token"])
    assert {row.title for row in search(db, 1, approved=True)} == {"Approved"}
    assert db.scalar(select(Knowledge).where(Knowledge.title == "Unverified")).tags == ["one", "two"]


def test_malformed_rows_require_explicit_rejection(db):
    content = csv_content(entry(title="", body="", category="Unknown", available_for_retrieval="maybe"),
                          entry(title="Good", verification="VERIFIED"))
    result = preview(db, 1, content, "csv")
    assert (result["valid"], result["invalid"]) == (1, 1)
    assert len(result["rows"][0]["errors"]) >= 4
    assert result["rows"][1]["record"]["verification"] == "PENDING"
    with pytest.raises(DomainError, match="explicitly choose"):
        commit(db, 1, content, "csv", result["preview_token"])
    summary = commit(db, 1, content, "csv", result["preview_token"], skip_invalid=True)
    assert (summary["created"], summary["rejected"]) == (1, 1)
    with pytest.raises(DomainError, match="expired"):
        commit(db, 1, content, "csv", result["preview_token"], skip_invalid=True)
    malformed = preview(db, 1, "not,json\n", "csv")
    assert malformed["file_errors"] and malformed["preview_token"] is None


def test_duplicate_detection_skip_update_and_upload_repeats(db):
    original = Knowledge(brand_id=1, title="A useful fact", category="Health", body="Detailed and sourced guidance.",
                         source="old", restrictions="old", tags=[], verification="APPROVED", enabled=True)
    db.add(original)
    db.commit()
    content = csv_content(entry(title=" A  USEFUL fact ", body="Revised body", source="new"),
                          entry(title="Second", body="Revised body", source="later"))
    first = preview(db, 1, content, "csv")
    assert first["duplicates"] == 2
    assert first["rows"][0]["matching_id"] == original.id
    assert first["rows"][1]["duplicate_of_row"] == 2
    skipped = commit(db, 1, content, "csv", first["preview_token"])
    assert skipped["skipped"] == 2 and original.source == "old"
    second = preview(db, 1, content, "csv")
    updated = commit(db, 1, content, "csv", second["preview_token"], mode="UPDATE_MATCHING")
    db.refresh(original)
    assert updated["updated"] == 1 and updated["skipped"] == 1
    assert original.body == "Revised body" and original.source == "new"
    assert db.scalars(select(Knowledge).where(Knowledge.brand_id == 1)).all() == [original]


def test_brand_isolation_and_stale_preview(db):
    db.add(Knowledge(brand_id=2, title="Shared", category="Health", body="Other brand body.",
                     source="other", restrictions="", tags=[], verification="APPROVED", enabled=True))
    db.commit()
    content = csv_content(entry(title="Shared", body="Different body"))
    result = preview(db, 1, content, "csv")
    assert result["duplicates"] == 0
    with pytest.raises(DomainError, match="another session/brand"):
        commit(db, 2, content, "csv", result["preview_token"])
    db.add(Knowledge(brand_id=1, title="Newly added", category="Health", body="Changes snapshot.",
                     source="", restrictions="", tags=[], verification="PENDING", enabled=False))
    db.commit()
    with pytest.raises(DomainError, match="changed after preview"):
        commit(db, 1, content, "csv", result["preview_token"])
    assert db.scalar(select(Knowledge).where(Knowledge.brand_id == 2)).body == "Other brand body."


@pytest.mark.parametrize("file_format", ["csv", "json"])
def test_export_reimport_round_trip(db, file_format):
    row = Knowledge(brand_id=1, title="Round trip", category="Finance", body="Line one\nLine two",
                    source="Book title, edition 2", restrictions="Use only as general education.",
                    tags=["long, useful tag", "finance"], verification="REJECTED", enabled=False)
    db.add(row)
    db.commit()
    original = bulk.export_rows(db, 1)
    content = bulk.export_csv(original) if file_format == "csv" else json.dumps(original)
    result = preview(db, 2, content, file_format)
    assert result["valid"] == 1 and result["invalid"] == 0
    commit(db, 2, content, file_format, result["preview_token"])
    assert bulk.export_rows(db, 2) == original
    assert search(db, 2, approved=True) == []


def test_large_import_uses_one_commit_and_batches(db, monkeypatch):
    rows = [entry(title=f"Item {number}", body=f"Distinct guidance {number}") for number in range(3000)]
    content = csv_content(*rows)
    result = preview(db, 1, content, "csv")
    calls = 0
    original_commit = db.commit

    def counted_commit():
        nonlocal calls
        calls += 1
        return original_commit()

    monkeypatch.setattr(db, "commit", counted_commit)
    summary = commit(db, 1, content, "csv", result["preview_token"])
    assert summary["created"] == 3000 and calls == 1
    assert db.scalar(select(Knowledge).where(Knowledge.brand_id == 1).limit(1)) is not None


def test_ambiguous_existing_matches_are_rejected(db):
    db.add_all([Knowledge(brand_id=1, title="Title match", category="Health", body="First body",
                          source="", restrictions="", tags=[], verification="PENDING", enabled=False),
                Knowledge(brand_id=1, title="Other title", category="Health", body="Second body",
                          source="", restrictions="", tags=[], verification="PENDING", enabled=False)])
    db.commit()
    content = csv_content(entry(title="Title match", body="Second body"))
    result = preview(db, 1, content, "csv")
    assert result["invalid"] == 1 and result["duplicates"] == 0
    assert "different existing records" in result["rows"][0]["errors"][0]


def test_import_and_export_http_routes(db):
    from fastapi import Request
    from fastapi.testclient import TestClient
    from app.db.session import get_db
    from app.main import app
    from app.security.auth import authenticated

    def test_db():
        yield db

    def test_admin(request: Request):
        request.state.session = SimpleNamespace(token_hash="test-session")
        return SimpleNamespace(id=10, username="admin")

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[authenticated] = test_admin
    try:
        client = TestClient(app)
        template = client.get("/api/brands/1/knowledge/import/template.csv")
        assert template.status_code == 200 and list(bulk.COLUMNS)[0] in template.text
        content = csv_content(entry(title="HTTP route"))
        response = client.post("/api/brands/1/knowledge/import/preview", json={"format": "csv", "content": content})
        assert response.status_code == 200 and response.json()["valid"] == 1
        token = response.json()["preview_token"]
        committed = client.post("/api/brands/1/knowledge/import/commit", json={"format": "csv", "content": content,
            "preview_token": token, "duplicate_mode": "SKIP_DUPLICATES"})
        assert committed.status_code == 200 and committed.json()["created"] == 1
        exported = client.get("/api/brands/1/knowledge/export.json")
        assert exported.status_code == 200 and exported.json()[0]["title"] == "HTTP route"
        assert client.get("/api/brands/2/knowledge/export.json").json() == []
    finally:
        app.dependency_overrides.clear()
