"""Local multipart registration tests; no YouTube provider requests."""
import hashlib
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db, utcnow
from app.main import app
from app.models import Admin, AdminSession, Brand, Content, YoutubeVideoAsset
from app.publishing import youtube
from app.storage.local import LocalStorage


PATH = "/api/brands/1/content/15/youtube-assets"


@pytest.fixture
def registration(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(youtube, "LocalStorage", lambda: LocalStorage(tmp_path))
    monkeypatch.setattr(youtube, "video_metadata", lambda path: {
        "duration_seconds": 4.0, "width": 1080, "height": 1920})
    with Session(engine) as db:
        admin = Admin(username="owner", password_hash="unused")
        db.add_all([Brand(id=1, name="LIFE, APPARENTLY.", slug="life-apparently", config={}),
            Content(id=15, brand_id=1, topic="Video", pillar="General", status="APPROVED", revision=11), admin])
        db.flush()
        token = "registration-test-session"
        db.add(AdminSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), admin_id=admin.id,
            csrf="csrf", expires_at=utcnow() + timedelta(hours=1)))
        db.commit()

        def test_db():
            yield db

        app.dependency_overrides[get_db] = test_db
        client = TestClient(app)
        client.cookies.set("dfb_session", token)
        try:
            yield db, client, tmp_path
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


def upload(client, *, revision="11", data=b"synthetic video", filename="finished.mp4"):
    return client.post(PATH, data={"revision": revision},
        files={"file": (filename, data, "video/mp4")}, headers={"x-csrf-token": "csrf"})


def test_valid_multipart_registers_current_approved_revision(registration, caplog, monkeypatch):
    db, client, root = registration
    monkeypatch.setattr(youtube.httpx, "Client", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("YouTube API must not be called during local registration")))
    with caplog.at_level(logging.INFO, logger="dfb.video_registration"):
        response = upload(client)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == 11 and result["byte_size"] == len(b"synthetic video")
    assert result["sha256"] == hashlib.sha256(b"synthetic video").hexdigest()
    assert "fields=['file', 'revision']" in caplog.text
    assert "upload_created=True" in caplog.text
    assert "content_type=multipart/form-data" in caplog.text
    assert "registration_store_complete" in caplog.text
    assert db.scalar(select(YoutubeVideoAsset)).content_id == 15
    assert list(root.rglob("*.mp4"))


@pytest.mark.parametrize("body,content_type", [
    (b"broken", "multipart/form-data; boundary=missing"),
    (b"broken", "multipart/form-data"),
])
def test_malformed_multipart_returns_sanitized_error_without_asset(registration, body, content_type, caplog):
    db, client, root = registration
    with caplog.at_level(logging.WARNING, logger="dfb.video_registration"):
        response = client.post(PATH, content=body, headers={"content-type": content_type, "x-csrf-token": "csrf"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Video upload request could not be parsed."}
    assert "registration_parse_failed exception=" in caplog.text
    assert "stored_bytes=0" in caplog.text
    assert not db.scalar(select(YoutubeVideoAsset))
    assert not list(root.rglob("*.mp4"))


def test_missing_file_and_empty_file(registration):
    db, client, root = registration
    missing = client.post(PATH, data={"revision": "11"},
        files={"other": ("x.txt", b"other")}, headers={"x-csrf-token": "csrf"})
    assert missing.status_code == 422
    empty = upload(client, data=b"")
    assert empty.status_code == 422 and "empty" in empty.json()["detail"].lower()
    assert not db.scalar(select(YoutubeVideoAsset))
    assert not list(root.rglob("*.mp4"))


def test_wrong_field_and_non_multipart_content_type(registration):
    db, client, _ = registration
    wrong_field = client.post(PATH, data={"revision": "11"},
        files={"video": ("finished.mp4", b"video", "video/mp4")}, headers={"x-csrf-token": "csrf"})
    assert wrong_field.status_code == 422
    json_request = client.post(PATH, json={"revision": 11}, headers={"x-csrf-token": "csrf"})
    assert json_request.status_code == 415
    assert not db.scalar(select(YoutubeVideoAsset))


def test_oversize_and_ffprobe_rejection_remove_partial_file(registration, monkeypatch):
    db, client, root = registration
    monkeypatch.setattr(youtube, "MAX_VIDEO_BYTES", 5)
    oversized = upload(client, data=b"123456")
    assert oversized.status_code == 413
    assert not list(root.rglob("*.mp4"))
    monkeypatch.setattr(youtube, "MAX_VIDEO_BYTES", 256 * 1024**3)
    monkeypatch.setattr(youtube, "video_metadata", lambda path: (_ for _ in ()).throw(
        youtube.DomainError("ffprobe could not inspect this video.", 422)))
    invalid = upload(client)
    assert invalid.status_code == 422
    assert not db.scalar(select(YoutubeVideoAsset))
    assert not list(root.rglob("*.mp4"))
    assert not list(root.rglob(".youtube-*"))


def test_revision_and_approval_gate(registration):
    db, client, root = registration
    stale = upload(client, revision="10")
    assert stale.status_code == 409 and "stale" in stale.json()["detail"].lower()
    db.get(Content, 15).status = "REVIEW"
    db.commit()
    not_approved = upload(client)
    assert not_approved.status_code == 409 and "Approve" in not_approved.json()["detail"]
    assert not db.scalar(select(YoutubeVideoAsset))
    assert not list(root.rglob("*.mp4"))
