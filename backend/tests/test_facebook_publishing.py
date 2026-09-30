"""Facebook Page plans and owner-confirmed receipts; every Meta request is faked."""
from datetime import timedelta
from types import SimpleNamespace
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.accounts.capabilities import facebook_publishing_issue
from app.api import content as content_api
from app.api.meta_auth import account_view
from app.core.errors import DomainError, ProviderError
from app.db.session import Base, utcnow
from app.models import Audit, Brand, Content, PlatformAccount, Publication, SystemSetting
from app.publishing import service
from app.publishing.providers import MetaPublisher, facebook_publication_plan
from app.publishing import public_media
from app.storage.local import LocalStorage


SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]


def account(scopes=SCOPES, status="healthy"):
    return PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
        token_encrypted="fake", config={"name": "LIFE, Apparently", "token_status": status,
                                        "last_checked": "now", "permissions": scopes, "tasks": ["CREATE_CONTENT"]})


def content(assets=None, status="APPROVED"):
    return Content(id=1, brand_id=1, topic="Test", pillar="General", status=status,
                   format="statement", assets=assets or [], caption="Caption", cta="", hashtags=[], body="",
                   targets=["facebook"], revision=1)


def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def ready(db):
    result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
        account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
    assert result["status"] == "READY", result["reasons"]
    return content_api.ManualPublishInput(platforms=["facebook"], account_ids={"facebook": 1},
        revision=1, exact_caption="Caption", media_keys=[], plan_token=result["plan_token"], confirmed=True)


def confirm(db, data):
    return content_api.manual_meta_publish(1, 1, data, admin=SimpleNamespace(username="owner"), db=db)


def test_missing_page_publish_scope_is_authorization_not_connection():
    page = account(scopes=["pages_show_list", "pages_read_engagement"])
    view = account_view(page)
    assert view["token_status"] == "healthy"
    assert view["publishing_status"] == "ADDITIONAL_AUTHORIZATION_REQUIRED"
    assert view["publishing_reason"] == "Additional authorization required: pages_manage_posts"
    assert facebook_publishing_issue(page) in service.publishing_blockers(content(), page, "facebook")
    assert account_view(account())["publishing_status"] == "READY"


def test_missing_permission_blocks_facebook_dry_run_without_disconnect():
    with Session(database()) as db:
        page = account(scopes=["pages_show_list", "pages_read_engagement"])
        db.add_all([Brand(id=1, name="Brand", slug="brand", config={}), content(), page])
        db.commit()
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "BLOCKED"
        assert "Additional authorization required: pages_manage_posts" in str(result["reasons"])
        assert account_view(page)["token_status"] == "healthy"
        assert not db.scalar(select(Publication))


def test_unhealthy_page_credential_blocks_publishing():
    page = account(status="unhealthy")
    assert account_view(page)["publishing_status"] == "UNHEALTHY"
    assert any("connection check" in reason for reason in service.publishing_blockers(content(), page, "facebook"))


def test_facebook_text_plan_and_feed_request(monkeypatch):
    item, page = content(), account()
    plan = facebook_publication_plan(item, page)
    assert plan["mode"] == "TEXT" and plan["page_id"] == "123"
    assert plan["graph_steps"] == [{"method": "POST", "path": "123/feed", "purpose": "publish_text"}]
    calls = []
    def request(self, account, method, path, data=None, *, outward=False):
        calls.append((method, path, data, outward))
        return {"id": "123_1"}
    monkeypatch.setattr(MetaPublisher, "request", request)
    assert MetaPublisher().publish(item, page, {}, lambda state: None) == "123_1"
    assert calls == [("POST", "123/feed", {"message": "Caption"}, True)]


def test_facebook_single_image_uses_page_photo_post(monkeypatch):
    item, page = content([{"key": "render/one.png"}]), account()
    assert facebook_publication_plan(item, page)["mode"] == "SINGLE_IMAGE"
    monkeypatch.setattr(MetaPublisher, "_media_url", lambda *args: "https://media.example/one.png")
    calls = []
    def request(self, account, method, path, data=None, *, outward=False):
        calls.append((method, path, data, outward))
        return {"id": "photo-1", "post_id": "123_2"}
    monkeypatch.setattr(MetaPublisher, "request", request)
    assert MetaPublisher().publish(item, page, {}, lambda state: None) == "123_2"
    assert calls == [("POST", "123/photos", {"url": "https://media.example/one.png",
                                                  "caption": "Caption", "published": "true"}, True)]


def test_facebook_multi_image_uses_unpublished_photos_and_indexed_attachments(monkeypatch):
    item, page = content([{"key": "render/one.png"}, {"key": "render/two.png"}]), account()
    plan = facebook_publication_plan(item, page)
    assert plan["mode"] == "MULTI_IMAGE"
    assert [step["purpose"] for step in plan["graph_steps"]] == ["upload_unpublished_photo"] * 2 + ["publish_attached_photos"]
    monkeypatch.setattr(MetaPublisher, "_media_url", lambda self, item, asset, *args: "https://media.example/" + asset["key"])
    calls, checkpoints = [], []
    def request(self, account, method, path, data=None, *, outward=False):
        calls.append((method, path, data, outward))
        return {"id": "photo-" + str(len(calls))} if path.endswith("/photos") else {"id": "123_3"}
    monkeypatch.setattr(MetaPublisher, "request", request)
    assert MetaPublisher().publish(item, page, {}, checkpoints.append) == "123_3"
    assert len(calls) == 3
    assert all(call[2]["published"] == "false" and call[3] for call in calls[:2])
    assert calls[2][0:2] == ("POST", "123/feed") and calls[2][3] is True
    assert calls[2][2]["attached_media[0]"] == '{"media_fbid": "photo-1"}'
    assert calls[2][2]["attached_media[1]"] == '{"media_fbid": "photo-2"}'
    assert checkpoints[-1]["outward_started"] is True


def test_page_post_without_confirmed_id_is_ambiguous(monkeypatch):
    monkeypatch.setattr(MetaPublisher, "request", lambda *args, **kwargs: {})
    with pytest.raises(ProviderError) as error:
        MetaPublisher().publish(content(), account(), {}, lambda state: None)
    assert error.value.uncertain


def test_invalid_render_is_blocked_before_facebook_media_upload(monkeypatch, tmp_path):
    data = b"\x89PNG\r\n\x1a\ninvalid"
    digest = hashlib.sha256(data).hexdigest()
    asset = {"key": f"brand/1/r1-1-{digest[:24]}.png", "sha256": digest}
    storage = LocalStorage(tmp_path)
    storage.write(asset["key"], data)
    monkeypatch.setattr(public_media, "LocalStorage", lambda: LocalStorage(tmp_path))
    with pytest.raises(ProviderError, match="INVALID_MEDIA"):
        public_media.approved_asset(content([asset]), asset)


def test_successful_facebook_receipt_on_already_published_instagram_content(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "inspect_account", lambda account: None)
    monkeypatch.setattr(MetaPublisher, "publish", lambda self, item, page, state, checkpoint: calls.append(page.platform) or "123_4")
    with Session(database()) as db:
        db.add_all([Brand(id=1, name="Brand", slug="brand", enabled=True, paused=False, config={}),
                    content(status="PUBLISHED"), account(), SystemSetting(key="autopilot", value={"paused": True}),
                    Publication(brand_id=1, content_id=1, platform="instagram", state="PUBLISHED", external_id="ig-1")])
        db.commit()
        data = ready(db)
        confirm(db, data)
        receipts = db.scalars(select(Publication).order_by(Publication.platform)).all()
        assert [(row.platform, row.state) for row in receipts] == [("facebook", "PUBLISHED"), ("instagram", "PUBLISHED")]
        fb = receipts[0]
        assert fb.external_id == "123_4" and fb.request_state["provider"] == "META_GRAPH"
        assert fb.request_state["page_id"] == "123" and fb.request_state["revision"] == 1
        assert fb.request_state["idempotency_key"] and fb.request_state["provider_response"] == {"id": "123_4"}
        assert fb.request_state["published_at"]
        assert db.get(Content, 1).status == "PUBLISHED" and db.get(SystemSetting, "autopilot").value["paused"]
        assert calls == ["facebook"]
        audit = db.scalar(select(Audit).where(Audit.action == "publication.published", Audit.target == "1").order_by(Audit.id.desc()))
        assert audit.details["action_source"] == "MANUAL_OWNER_CONFIRMED"
        with pytest.raises(DomainError):
            confirm(db, data)
        assert calls == ["facebook"]


def test_ambiguous_facebook_result_preserves_instagram_receipt_and_blocks_retry(monkeypatch):
    monkeypatch.setattr(service, "inspect_account", lambda account: None)
    calls = []
    def ambiguous(self, item, page, state, checkpoint):
        calls.append(1)
        raise ProviderError("Facebook outcome unknown", uncertain=True)
    monkeypatch.setattr(MetaPublisher, "publish", ambiguous)
    with Session(database()) as db:
        db.add_all([Brand(id=1, name="Brand", slug="brand", enabled=True, paused=False, config={}),
                    content(status="PUBLISHED"), account(), SystemSetting(key="autopilot", value={"paused": True}),
                    Publication(brand_id=1, content_id=1, platform="instagram", state="PUBLISHED", external_id="ig-1")])
        db.commit()
        data = ready(db)
        with pytest.raises(ProviderError) as error:
            confirm(db, data)
        assert error.value.uncertain
        fb = db.scalar(select(Publication).where(Publication.platform == "facebook"))
        assert fb.state == "UNKNOWN" and fb.attempts == 1
        assert db.get(Content, 1).status == "PUBLISHED"
        second = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert second["status"] == "BLOCKED" and "reconciliation" in str(second["reasons"])
        assert calls == [1]


def test_facebook_ready_plan_rejects_stale_revision_caption_page_and_expiry(monkeypatch):
    monkeypatch.setattr(service, "inspect_account", lambda account: None)
    monkeypatch.setattr(MetaPublisher, "publish", lambda *args: (_ for _ in ()).throw(AssertionError("Meta mutation")))
    with Session(database()) as db:
        db.add_all([Brand(id=1, name="Brand", slug="brand", enabled=True, paused=False, config={}),
                    content(), account(), SystemSetting(key="autopilot", value={"paused": True})])
        db.commit()
        data = ready(db)
        data.revision = 2
        with pytest.raises(DomainError, match="changed"):
            confirm(db, data)
        data.revision = 1
        db.get(Content, 1).caption = "New caption"
        db.commit()
        with pytest.raises(DomainError, match="changed"):
            confirm(db, data)
        db.get(Content, 1).caption = "Caption"
        db.get(Content, 1).assets = [{"key": "changed.png"}]
        db.commit()
        with pytest.raises(DomainError, match="changed"):
            confirm(db, data)
        db.get(Content, 1).assets = []
        db.get(PlatformAccount, 1).account_id = "456"
        db.commit()
        with pytest.raises(DomainError, match="destination"):
            confirm(db, data)
        db.get(PlatformAccount, 1).account_id = "123"
        plan = db.get(SystemSetting, "manual_publish_plan:" + data.plan_token)
        plan.value = {**plan.value, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
        db.commit()
        with pytest.raises(DomainError, match="expired"):
            confirm(db, data)
        assert not db.scalar(select(Publication))
