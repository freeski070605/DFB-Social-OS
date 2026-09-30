import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, SSLError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from PIL import Image

from app.core.errors import ProviderError
from app.db.session import Base
from app.models import Brand, Content, PlatformAccount, Publication, SystemSetting
from app.publishing import public_media, service
from app.api import content as content_api
from app.storage.local import LocalStorage


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.fail_upload = False
        self.fail_delete = False

    def put_object(self, **kwargs):
        if self.fail_upload:
            raise RuntimeError("secret upstream response")
        self.objects[kwargs["Key"]] = kwargs

    def delete_object(self, **kwargs):
        if self.fail_delete:
            raise RuntimeError("secret upstream response")
        self.deleted.append(kwargs["Key"])
        self.objects.pop(kwargs["Key"], None)


def configured(monkeypatch, tmp_path):
    cfg = SimpleNamespace(public_media_provider="r2", r2_account_id="a" * 32,
        r2_access_key_id="private-key", r2_secret_access_key="private-secret",
        r2_bucket="publish-only", r2_public_base_url="https://media.example.com")
    monkeypatch.setattr(public_media, "settings", lambda: cfg)
    monkeypatch.setattr(public_media, "LocalStorage", lambda: LocalStorage(tmp_path))
    return cfg


def rendered(tmp_path, extension="png", data=None):
    if data is None:
        output = BytesIO()
        Image.new("RGB", (10, 10), "white").save(output, format="JPEG" if extension in {"jpg", "jpeg"} else "PNG")
        data = output.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    asset = {"key": f"brand/1/r1-1-{digest[:24]}.{extension}", "sha256": digest}
    LocalStorage(tmp_path).write(asset["key"], data)
    return SimpleNamespace(id=1, revision=1, status="APPROVED", assets=[asset]), asset


def test_r2_upload_mime_url_rejection_and_delete(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    fake = FakeR2()
    provider = public_media.R2PublicMediaProvider(fake)
    item, asset = rendered(tmp_path)
    key, url = provider.prepare_object(item, asset)
    assert key.startswith("meta-publish/") and url == "https://media.example.com/" + key
    assert fake.objects[key]["ContentType"] == "image/png"
    assert fake.objects[key]["CacheControl"] == "no-store"
    provider.delete(key)
    assert fake.deleted == [key]
    with pytest.raises(ProviderError, match="Invalid temporary"):
        provider.delete("../private-object")
    with pytest.raises(ProviderError, match="Only approved rendered"):
        provider.prepare_object(item, {"key": "../../secret.png"})
    jpeg, image = rendered(tmp_path, "jpg")
    key, _ = provider.prepare_object(jpeg, image)
    assert fake.objects[key]["ContentType"] == "image/jpeg"
    unsupported, wrong = rendered(tmp_path, "png", b"not an image")
    with pytest.raises(ProviderError, match="INVALID_MEDIA"):
        provider.prepare_object(unsupported, wrong)
    fake.fail_upload = True
    with pytest.raises(ProviderError, match="R2 media upload failed") as error:
        provider.prepare_object(item, asset)
    assert error.value.message.startswith("R2_PUT_FAILED:")
    assert "private" not in str(error.value)


def test_r2_tls_failure_has_safe_actionable_category(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)

    class TlsFailure(FakeR2):
        def put_object(self, **kwargs):
            raise SSLError(endpoint_url="https://private.example", error="secret upstream response")

    item, asset = rendered(tmp_path)
    with pytest.raises(ProviderError, match="R2_ENDPOINT_TLS_FAILED") as error:
        public_media.R2PublicMediaProvider(TlsFailure()).prepare_object(item, asset)
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_instagram_rendered_png_is_uploaded_as_jpeg(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    output = BytesIO()
    Image.new("RGB", (1080, 1350), "white").save(output, format="PNG")
    item, asset = rendered(tmp_path, data=output.getvalue())
    fake = FakeR2()
    key, _ = public_media.R2PublicMediaProvider(fake).prepare_object(item, asset, "instagram")
    assert key.endswith(".jpg")
    assert fake.objects[key]["ContentType"] == "image/jpeg"
    assert fake.objects[key]["Body"].startswith(b"\xff\xd8\xff")


def test_r2_requires_public_https_hostname(monkeypatch, tmp_path):
    cfg = configured(monkeypatch, tmp_path)
    cfg.r2_public_base_url = "https://a" + "a" * 31 + ".r2.cloudflarestorage.com"
    assert not public_media.r2_ready()
    with pytest.raises(ProviderError):
        public_media.R2PublicMediaProvider(FakeR2())


def test_r2_dev_public_hostname_is_ready_and_endpoint_url_is_not_an_account_id(monkeypatch, tmp_path):
    cfg = configured(monkeypatch, tmp_path)
    cfg.r2_public_base_url = "https://pub-" + "a" * 32 + ".r2.dev"
    assert public_media.r2_ready()
    assert public_media.R2PublicMediaProvider(FakeR2()).base_url == cfg.r2_public_base_url
    cfg.r2_account_id = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
    assert public_media.r2_readiness_issue() == "R2 account ID format is invalid"


def test_r2_upload_reports_safe_credential_failure(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)

    class InvalidCredentials(FakeR2):
        def put_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "SignatureDoesNotMatch", "Message": "secret upstream response"}},
                              "PutObject")

    item, asset = rendered(tmp_path)
    with pytest.raises(ProviderError, match="R2 credentials are invalid") as error:
        public_media.R2PublicMediaProvider(InvalidCredentials()).prepare_object(item, asset)
    assert "secret" not in str(error.value)


def test_dry_run_reports_r2_configuration_issue(monkeypatch, tmp_path):
    cfg = configured(monkeypatch, tmp_path)
    cfg.r2_account_id = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
    _, asset = rendered(tmp_path)
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                       format="statement", assets=[asset], caption="Caption", targets=["instagram"]))
        db.add(PlatformAccount(id=1, brand_id=1, platform="instagram", account_id="123", enabled=True,
                               token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                   "permissions": ["pages_show_list", "pages_read_engagement", "instagram_basic",
                                                   "instagram_content_publish"]}))
        db.commit()
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["instagram"],
            account_ids={"instagram": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "BLOCKED"
        assert "instagram: R2 account ID format is invalid" in result["reasons"]


def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_dry_run_blocks_missing_permission_and_never_calls_meta(monkeypatch):
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                       format="statement", assets=[], caption="Caption", targets=["facebook"]))
        db.add(PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                               token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                               "permissions": []}))
        db.commit()
        monkeypatch.setattr(content_api, "public_media_provider", lambda: (_ for _ in ()).throw(AssertionError("upload")))
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "BLOCKED"
        assert "pages_manage_posts" in str(result["reasons"])
        assert not db.query(Publication).count()


def test_facebook_text_dry_run_ready_without_meta_mutation(monkeypatch):
    from app.publishing.providers import MetaPublisher
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                       format="statement", assets=[], caption="Caption", targets=["facebook"]))
        db.add(PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                               token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                               "permissions": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]}))
        db.commit()
        monkeypatch.setattr(MetaPublisher, "request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Meta")))
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "READY" and result["plan"][0]["action"] == "Facebook Page text"
        assert db.get(SystemSetting, "manual_publish_plan:" + result["plan_token"])
        assert not db.query(Publication).count()
        data = content_api.ManualPublishInput(platforms=["facebook"], account_ids={"facebook": 1},
            revision=1, exact_caption="Caption", media_keys=[], plan_token=result["plan_token"], confirmed=True)
        with pytest.raises(Exception, match="Brand outward actions paused"):
            content_api.manual_meta_publish(1, 1, data, admin=SimpleNamespace(username="owner"), db=db)
        assert not db.query(Publication).count()
        assert db.get(SystemSetting, "manual_publish_plan:" + result["plan_token"])


def test_image_dry_run_verifies_url_and_cleans_without_meta(monkeypatch, tmp_path):
    from app.publishing.providers import MetaPublisher
    configured(monkeypatch, tmp_path)
    _, asset = rendered(tmp_path)
    fake = FakeR2()
    monkeypatch.setattr(MetaPublisher, "request", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Meta")))
    monkeypatch.setattr(content_api, "public_media_provider", lambda: public_media.R2PublicMediaProvider(fake))

    class HeadClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def head(self, url):
            assert url.startswith("https://media.example.com/meta-publish/")
            return SimpleNamespace(status_code=200, headers={"content-type": "image/png"})

    monkeypatch.setattr(content_api.httpx, "Client", HeadClient)
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                       format="statement", assets=[asset], caption="Caption", targets=["facebook"]))
        db.add(PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                               token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                               "permissions": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]}))
        db.commit()
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "READY"
        assert public_media.r2_verified(db)
        assert fake.deleted and not fake.objects
        assert not db.query(Publication).count()


def test_dry_run_exposes_tls_category_without_upstream_details(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    output = BytesIO()
    Image.new("RGB", (10, 10), "white").save(output, format="PNG")
    _, asset = rendered(tmp_path, data=output.getvalue())

    class TlsFailure(FakeR2):
        def put_object(self, **kwargs):
            raise SSLError(endpoint_url="https://private.example", error="secret upstream response")

    monkeypatch.setattr(content_api, "public_media_provider", lambda: public_media.R2PublicMediaProvider(TlsFailure()))
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                       format="statement", assets=[asset], caption="Caption", targets=["instagram"]))
        db.add(PlatformAccount(id=1, brand_id=1, platform="instagram", account_id="123", enabled=True,
                               token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                   "permissions": ["pages_show_list", "pages_read_engagement", "instagram_basic",
                                                   "instagram_content_publish"]}))
        db.commit()
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["instagram"],
            account_ids={"instagram": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "BLOCKED"
        assert any(reason.startswith("R2_ENDPOINT_TLS_FAILED:") for reason in result["reasons"])
        assert "private" not in str(result) and "secret" not in str(result)
        assert not db.query(Publication).count()


def test_scheduler_does_not_delete_active_dry_run_intent(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    _, asset = rendered(tmp_path)
    engine = database()

    class SweepingR2(FakeR2):
        def put_object(self, **kwargs):
            with Session(engine) as sweeper:
                service.cleanup_media(sweeper)
            super().put_object(**kwargs)

    fake = SweepingR2()
    monkeypatch.setattr(content_api, "public_media_provider", lambda: public_media.R2PublicMediaProvider(fake))
    monkeypatch.setattr(public_media, "public_media_provider", lambda: public_media.R2PublicMediaProvider(fake))

    class HeadClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def head(self, url): return SimpleNamespace(status_code=200, headers={"content-type": "image/png"})

    monkeypatch.setattr(content_api.httpx, "Client", HeadClient)
    with Session(engine) as db:
        db.add_all([Brand(id=1, name="Brand", slug="brand", config={}),
                    Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                            format="statement", assets=[asset], caption="Caption", targets=["facebook"]),
                    PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                                    token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                                           "permissions": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]})])
        db.commit()
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "READY"
        assert fake.deleted and not fake.objects


def test_scheduler_cleans_expired_dry_run_intent(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    fake = FakeR2()
    monkeypatch.setattr(public_media, "public_media_provider", lambda: public_media.R2PublicMediaProvider(fake))
    key = "meta-publish/" + "a" * 32 + ".jpg"
    with Session(database()) as db:
        db.add(SystemSetting(key="dryrun_media:expired", value={"objects": [key],
                                                           "expires_at": "2000-01-01T00:00:00"}))
        db.commit()
        service.cleanup_media(db)
        assert fake.deleted == [key]
        assert db.get(SystemSetting, "dryrun_media:expired") is None


def test_dry_run_logs_first_stale_row_error_and_rolls_back(monkeypatch, tmp_path, caplog):
    configured(monkeypatch, tmp_path)
    _, asset = rendered(tmp_path)
    engine = database()

    class RemovingR2(FakeR2):
        def put_object(self, **kwargs):
            with Session(engine) as remover:
                pending = remover.scalar(select(SystemSetting).where(SystemSetting.key.like("dryrun_media:%")))
                db.get(SystemSetting, pending.key).value  # Load the active row before the competing delete.
                remover.delete(pending)
                remover.commit()
            super().put_object(**kwargs)

    fake = RemovingR2()
    monkeypatch.setattr(content_api, "public_media_provider", lambda: public_media.R2PublicMediaProvider(fake))
    with Session(engine) as db:
        db.add_all([Brand(id=1, name="Brand", slug="brand", config={}),
                    Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                            format="statement", assets=[asset], caption="Caption", targets=["facebook"]),
                    PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                                    token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                                           "permissions": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]})])
        db.commit()
        result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(platforms=["facebook"],
            account_ids={"facebook": 1}), admin=SimpleNamespace(username="owner"), db=db)
        assert result["status"] == "BLOCKED"
        assert any(reason.startswith("DRY_RUN_DB_FAILED:") for reason in result["reasons"])
        assert "PendingRollbackError" not in caplog.text
        assert "StaleDataError" in caplog.text and "stage=media_checkpoint" in caplog.text
        assert "expected to update 1 row(s); 0 were matched" in caplog.text
        assert fake.deleted and not fake.objects


def test_cleanup_retries_after_restart(monkeypatch):
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="PUBLISHED"))
        db.add(Publication(id=1, brand_id=1, content_id=1, platform="facebook", state="PUBLISHED",
                           request_state={"media_objects": ["meta-publish/" + "a" * 32 + ".png"],
                                          "cleanup_after": "2000-01-01T00:00:00"}))
        db.commit()
        fake = FakeR2()
        monkeypatch.setattr(public_media, "settings", lambda: SimpleNamespace(public_media_provider="r2",
            r2_account_id="a" * 32, r2_access_key_id="key", r2_secret_access_key="secret",
            r2_bucket="publish-only", r2_public_base_url="https://media.example.com"))
        # cleanup imports the factory directly, so replace it on the module.
        monkeypatch.setattr(public_media, "public_media_provider", lambda: public_media.R2PublicMediaProvider(fake))
        fake.fail_delete = True
        service.cleanup_media(db)
        assert db.get(Publication, 1).request_state["media_objects"]
        fake.fail_delete = False
        service.cleanup_media(db)
        assert not db.get(Publication, 1).request_state["media_objects"]


def test_repeated_manual_publish_uses_one_receipt_and_one_external_action(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "outward_allowed", lambda db, brand: None)
    monkeypatch.setattr(service, "inspect_account", lambda account: None)

    def fake_publish(self, item, account, state, checkpoint):
        calls.append((item.id, account.id))
        return "external-123"

    monkeypatch.setattr(service.MetaPublisher, "publish", fake_publish)
    with Session(database()) as db:
        db.add(Brand(id=1, name="Brand", slug="brand", config={}))
        db.add(Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                       format="statement", assets=[], caption="Caption", targets=["facebook"]))
        db.add(PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                               token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                               "permissions": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]}))
        db.commit()
        service.publish(db, 1, 1, "owner", ["facebook"], {"facebook": 1})
        service.publish(db, 1, 1, "owner", ["facebook"], {"facebook": 1})
        receipts = db.query(Publication).all()
        assert calls == [(1, 1)]
        assert len(receipts) == 1 and receipts[0].state == "PUBLISHED"
        assert receipts[0].external_id == "external-123"
        assert len(receipts[0].request_state["idempotency_key"]) == 32
