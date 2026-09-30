"""Private YouTube publishing safety and resumable-upload tests; no live provider calls."""
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.errors import DomainError
from app.db.session import Base, utcnow
from app.models import (Audit, Brand, Content, PlatformAccount, Publication, SystemSetting,
                        YoutubeUploadAttempt)
from app.publishing import youtube


class FakeProvider:
    def __init__(self, *, processing="succeeded", ambiguous=False):
        self.processing = processing
        self.ambiguous = ambiguous
        self.initiations = 0
        self.chunks = []
        self.session_queries = 0
        self.metadata = None

    def initiate(self, token, attempt, asset):
        self.initiations += 1
        self.metadata = dict(attempt.upload_metadata)
        return "https://upload.example.test/session/opaque"

    def query_session(self, token, session_uri, total):
        self.session_queries += 1
        return 0, ""

    def send_chunk(self, token, session_uri, offset, chunk, total):
        self.chunks.append((offset, bytes(chunk)))
        if self.ambiguous:
            raise youtube.YoutubeProviderError("uncertain test result", uncertain=True)
        return total, "fake-youtube-id"

    def processing_status(self, token, video_id):
        return self.processing


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    storage_type = youtube.LocalStorage
    monkeypatch.setattr(youtube, "LocalStorage", lambda: storage_type(tmp_path))
    monkeypatch.setattr(youtube, "video_metadata", lambda path: {
        "duration_seconds": 12.5, "width": 1080, "height": 1920})
    monkeypatch.setattr(youtube, "decrypt", lambda value: "test-access-token")
    now = int(utcnow().timestamp())
    with Session(engine) as db:
        db.add_all([
            Brand(id=1, name="LIFE, APPARENTLY.", slug="life-apparently", enabled=True, paused=False, config={}),
            Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                    format="short_video_script", revision=3, assets=[], caption="Approved script"),
            PlatformAccount(id=1, brand_id=1, platform="youtube", account_id="channel-1", enabled=True,
                token_encrypted="encrypted-test", config={"token_status": "healthy", "last_checked": "now",
                    "expires_at": now + 3600, "name": "LIFE, APPARENTLY.",
                    "permissions": [youtube.UPLOAD_SCOPE]}),
            SystemSetting(key="autopilot", value={"paused": True}),
        ])
        db.commit()
        yield db
    engine.dispose()


def make_asset(db, monkeypatch, data=b"fake-video-content", filename="finished.mp4"):
    asset = youtube.store_video_asset(db, db.get(Content, 1),
        SimpleNamespace(filename=filename, file=BytesIO(data)))
    return asset


def ready(db, asset, *, fmt="YOUTUBE_SHORT", audience="NOT_MADE_FOR_KIDS"):
    return youtube.dry_run(db, 1, 1, 1, asset.id, "A clear title", "A complete video description.", fmt, audience)


def confirmation(plan, *, title=None, description=None, audience=None):
    return {"revision": plan["revision"], "account_id": plan["account_id"], "asset_id": plan["asset_id"],
        "title": title if title is not None else plan["title"],
        "description": description if description is not None else plan["description"],
        "intended_format": plan["intended_format"], "audience": audience if audience is not None else plan["audience"],
        "privacy": "PRIVATE", "plan_token": plan["plan_token"], "confirmed": True}


def test_asset_is_revision_bound_hashed_and_stream_saved(setup, monkeypatch):
    db = setup
    payload = b"video bytes in a local file"
    asset = make_asset(db, monkeypatch, payload)
    assert asset.revision == 3
    assert asset.byte_size == len(payload)
    assert asset.sha256 == __import__("hashlib").sha256(payload).hexdigest()
    assert asset.duration_seconds == 12.5 and (asset.width, asset.height) == (1080, 1920)
    assert youtube.LocalStorage().path(asset.storage_key).read_bytes() == payload


@pytest.mark.parametrize("filename,payload,error", [
    ("missing.mp4", b"", "empty"),
    ("notes.txt", b"not a video", "supported video"),
])
def test_missing_or_unsupported_video_is_rejected(setup, filename, payload, error):
    with pytest.raises(DomainError, match=error):
        make_asset(setup, None, payload, filename)


def test_invalid_video_is_rejected_and_not_kept(setup, monkeypatch):
    monkeypatch.setattr(youtube, "video_metadata", lambda path: (_ for _ in ()).throw(DomainError("invalid video")))
    with pytest.raises(DomainError, match="invalid video"):
        make_asset(setup, monkeypatch)
    assert not list(youtube.LocalStorage().root.rglob("*.mp4"))


def test_dry_run_blocks_readonly_and_unhealthy_accounts(setup):
    db = setup
    asset = make_asset(db, None)
    account = db.get(PlatformAccount, 1)
    account.config = {**account.config, "permissions": []}
    db.commit()
    assert "Additional authorization" in " ".join(ready(db, asset)["reasons"])
    account.config = {**account.config, "permissions": [youtube.UPLOAD_SCOPE], "token_status": "unhealthy"}
    db.commit()
    assert "unhealthy" in " ".join(ready(db, asset)["reasons"]).lower()


def test_dry_run_blocks_missing_video_audience_and_inactive_brand(setup):
    db = setup
    missing = youtube.dry_run(db, 1, 1, 1, 999, "Title", "Description", "YOUTUBE_SHORT", "NOT_MADE_FOR_KIDS")
    assert missing["status"] == "BLOCKED" and any("video" in reason.lower() for reason in missing["reasons"])
    asset = make_asset(db, None)
    no_audience = ready(db, asset, audience=None)
    assert no_audience["status"] == "BLOCKED" and any("audience" in reason.lower() for reason in no_audience["reasons"])
    db.get(Brand, 1).paused = True
    db.commit()
    assert any("active" in reason for reason in ready(db, asset)["reasons"])


def test_metadata_constraints_and_stale_asset_revision_block_ready(setup):
    db = setup
    asset = make_asset(db, None)
    too_long = youtube.dry_run(db, 1, 1, 1, asset.id, "T" * 101, "Description", "YOUTUBE_SHORT", "NOT_MADE_FOR_KIDS")
    assert too_long["status"] == "BLOCKED"
    too_long = youtube.dry_run(db, 1, 1, 1, asset.id, "Title", "D" * 5001, "YOUTUBE_SHORT", "NOT_MADE_FOR_KIDS")
    assert too_long["status"] == "BLOCKED"
    db.get(Content, 1).revision = 4
    db.commit()
    stale = ready(db, asset)
    assert stale["status"] == "BLOCKED" and any("current content revision" in reason for reason in stale["reasons"])


@pytest.mark.parametrize("fmt", ["YOUTUBE_SHORT", "YOUTUBE_LONGFORM"])
def test_ready_plan_binds_private_metadata_and_format(setup, fmt):
    plan = ready(setup, make_asset(setup, None), fmt=fmt)
    assert plan["status"] == "READY"
    assert plan["intended_format"] == fmt
    assert plan["privacy"] == "PRIVATE"
    assert plan["sha256"] and plan["byte_size"] > 0
    assert "storage_key" not in plan


def test_expired_plan_and_changed_metadata_or_video_fail_confirmation(setup):
    db = setup
    asset = make_asset(db, None)
    plan = ready(db, asset)
    with pytest.raises(DomainError, match="metadata changed"):
        youtube.confirm(db, 1, 1, confirmation(plan, title="Changed title"), "owner", FakeProvider())
    path = youtube.LocalStorage().path(asset.storage_key)
    path.write_bytes(b"changed file bytes")
    with pytest.raises(DomainError, match="changed after selection"):
        youtube.confirm(db, 1, 1, confirmation(plan), "owner", FakeProvider())
    path.write_bytes(b"fake-video-content")
    saved = db.get(SystemSetting, "youtube_manual_plan:" + plan["plan_token"])
    saved.value = {**saved.value, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    db.commit()
    with pytest.raises(DomainError, match="expired"):
        youtube.confirm(db, 1, 1, confirmation(plan), "owner", FakeProvider())


def test_global_autonomous_pause_does_not_block_owner_confirmation(setup):
    db = setup
    asset = make_asset(db, None)
    plan = ready(db, asset)
    provider = FakeProvider()
    result = youtube.confirm(db, 1, 1, confirmation(plan), "owner", provider)
    assert result["state"] == "PROCESSING"
    assert provider.initiations == 1
    assert db.get(SystemSetting, "autopilot").value["paused"] is True


def test_brand_pause_blocks_owner_confirmation(setup):
    db = setup
    asset = make_asset(db, None)
    plan = ready(db, asset)
    db.get(Brand, 1).paused = True
    db.commit()
    with pytest.raises(DomainError, match="Brand outward actions paused"):
        youtube.confirm(db, 1, 1, confirmation(plan), "owner", FakeProvider())


def test_ambiguous_upload_requires_reconciliation_and_never_retries(setup):
    db = setup
    asset = make_asset(db, None)
    plan = ready(db, asset)
    provider = FakeProvider(ambiguous=True)
    result = youtube.confirm(db, 1, 1, confirmation(plan), "owner", provider)
    assert result["state"] == "RECONCILIATION_REQUIRED"
    assert provider.initiations == 1 and len(provider.chunks) == 1
    attempt = db.scalar(select(YoutubeUploadAttempt))
    assert attempt.session_uri and attempt.state == "RECONCILIATION_REQUIRED"
    assert not db.scalar(select(Publication).where(Publication.platform == "youtube"))
    assert any("attempt already exists" in reason.lower() for reason in ready(db, asset)["reasons"])
    with pytest.raises(DomainError, match="saved resumable session can be resumed"):
        youtube.resume(db, 1, attempt.id, "owner", FakeProvider())


def test_duplicate_confirmation_never_starts_a_second_session(setup):
    db = setup
    asset = make_asset(db, None)
    plan = ready(db, asset)
    provider = FakeProvider()
    data = confirmation(plan)
    youtube.confirm(db, 1, 1, data, "owner", provider)
    with pytest.raises(DomainError, match="READY plan expired"):
        youtube.confirm(db, 1, 1, data, "owner", provider)
    assert provider.initiations == 1
    assert len(db.scalars(select(YoutubeUploadAttempt)).all()) == 1
    assert db.scalar(select(Publication).where(Publication.platform == "youtube")).external_id == "fake-youtube-id"


def test_interrupted_attempt_resumes_same_session_without_reinitializing(setup):
    db = setup
    asset = make_asset(db, None)
    attempt = YoutubeUploadAttempt(brand_id=1, content_id=1, revision=3, asset_id=asset.id,
        asset_sha256=asset.sha256, account_id=1, channel_id="channel-1", idempotency_key="a" * 64,
        state="UPLOADING", session_uri="https://upload.example.test/session/existing", bytes_sent=0,
        upload_metadata={"title": "A clear title", "description": "A complete video description.",
            "intended_format": "YOUTUBE_SHORT", "audience": "NOT_MADE_FOR_KIDS", "privacy": "PRIVATE",
            "filename": asset.filename, "asset_sha256": asset.sha256, "byte_size": asset.byte_size})
    db.add(attempt)
    db.commit()
    provider = FakeProvider()
    result = youtube.resume(db, 1, attempt.id, "owner", provider)
    assert result["state"] == "PROCESSING"
    assert provider.initiations == 0 and provider.session_queries == 1
    assert provider.chunks and db.get(YoutubeUploadAttempt, attempt.id).session_uri.endswith("existing")


def test_successful_video_id_receipt_private_processing_and_other_receipts(setup):
    db = setup
    instagram = Publication(brand_id=1, content_id=1, platform="instagram", external_id="ig-1",
                            state="PUBLISHED", request_state={"legacy": "kept"})
    facebook = Publication(brand_id=1, content_id=1, platform="facebook", external_id="fb-1",
                           state="PUBLISHED", request_state={"legacy": "kept"})
    db.add_all([instagram, facebook])
    db.commit()
    asset = make_asset(db, None)
    plan = ready(db, asset, fmt="YOUTUBE_LONGFORM", audience="MADE_FOR_KIDS")
    provider = FakeProvider()
    attempt = youtube.confirm(db, 1, 1, confirmation(plan), "owner", provider)
    assert attempt["state"] == "PROCESSING"
    assert attempt["provider_video_id"] == "fake-youtube-id"
    assert provider.metadata["privacy"] == "PRIVATE"
    receipt = db.scalar(select(Publication).where(Publication.platform == "youtube"))
    assert receipt.state == "PROCESSING" and receipt.external_id == "fake-youtube-id"
    assert receipt.request_state["provider"] == "youtube"
    assert receipt.request_state["privacy"] == "PRIVATE"
    assert receipt.request_state["intended_format"] == "YOUTUBE_LONGFORM"
    assert receipt.request_state["audience"] == "MADE_FOR_KIDS"
    assert "token" not in str(receipt.request_state).lower()
    confirmation_audit = db.scalar(select(Audit).where(Audit.action == "publication.manual_confirmation"))
    assert confirmation_audit.details["action_source"] == "MANUAL_OWNER_CONFIRMED"
    assert youtube.status(db, 1, 1, provider)["state"] == "PUBLISHED"
    assert db.get(Publication, instagram.id).request_state == {"legacy": "kept"}
    assert db.get(Publication, facebook.id).request_state == {"legacy": "kept"}
    assert db.get(Publication, receipt.id).state == "PUBLISHED"


def test_processing_state_does_not_claim_completion(setup):
    db = setup
    asset = make_asset(db, None)
    plan = ready(db, asset)
    provider = FakeProvider(processing="processing")
    upload = youtube.confirm(db, 1, 1, confirmation(plan), "owner", provider)
    assert upload["state"] == "PROCESSING"
    assert youtube.status(db, 1, 1, provider)["state"] == "PROCESSING"
    assert db.scalar(select(Publication).where(Publication.platform == "youtube")).state == "PROCESSING"


def test_official_request_uses_private_resumable_metadata_without_network():
    class FakeResponse:
        status_code = 200
        headers = {"Location": "https://upload.example.test/session/new"}

    class FakeHttp:
        def post(self, url, **kwargs):
            self.url, self.kwargs = url, kwargs
            return FakeResponse()

    http = FakeHttp()
    provider = youtube.YouTubeResumableProvider(client=http)
    attempt = SimpleNamespace(upload_metadata={"title": "Title", "description": "Description",
                                                "audience": "MADE_FOR_KIDS"})
    asset = SimpleNamespace(filename="video.mp4", byte_size=12)
    session = provider.initiate("not-a-real-token", attempt, asset)
    body = http.kwargs["json"]
    assert session.endswith("new")
    assert "uploadType" in http.kwargs["params"] and http.kwargs["params"]["uploadType"] == "resumable"
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is True
    assert http.kwargs["headers"]["X-Upload-Content-Length"] == "12"