"""Manual publishing gates, using a fake publisher without Meta requests."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import content as content_api
from app.core.errors import DomainError, ProviderError
from app.db.session import Base, utcnow
from app.models import Audit, Brand, Content, Job, PlatformAccount, Publication, SystemSetting
from app.publishing import service
from app.scheduling.worker import execute


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    calls = []

    def fake_publish(self, item, account, state, checkpoint):
        calls.append((item.id, account.id))
        return "mock-post-1"

    monkeypatch.setattr(service, "inspect_account", lambda account: None)
    monkeypatch.setattr(service.MetaPublisher, "publish", fake_publish)
    with Session(engine) as db:
        db.add_all([
            Brand(id=1, name="Brand", slug="brand", enabled=True, paused=False, config={}),
            Content(id=1, brand_id=1, topic="Test", pillar="General", status="APPROVED",
                    format="statement", assets=[], caption="Caption", targets=["facebook"]),
            PlatformAccount(id=1, brand_id=1, platform="facebook", account_id="123", enabled=True,
                            token_encrypted="encrypted", config={"token_status": "healthy", "last_checked": "now",
                                                          "permissions": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"]}),
            SystemSetting(key="autopilot", value={"paused": True}),
        ])
        db.commit()
        yield db, calls


def ready(db):
    result = content_api.publish_dry_run(1, 1, content_api.DryRunInput(
        platforms=["facebook"], account_ids={"facebook": 1}),
        admin=SimpleNamespace(username="owner"), db=db)
    assert result["status"] == "READY"
    return content_api.ManualPublishInput(platforms=["facebook"], account_ids={"facebook": 1},
        revision=1, exact_caption="Caption", media_keys=[], plan_token=result["plan_token"], confirmed=True)


def confirm(db, data):
    return content_api.manual_meta_publish(1, 1, data, admin=SimpleNamespace(username="owner"), db=db)


@pytest.mark.parametrize("global_paused", [True, False])
def test_manual_allowed_with_active_brand_regardless_of_global_pause(setup, global_paused):
    db, calls = setup
    db.get(SystemSetting, "autopilot").value = {"paused": global_paused}
    db.commit()
    data = ready(db)
    confirm(db, data)
    receipt = db.scalar(select(Publication))
    assert calls == [(1, 1)]
    assert receipt.state == "PUBLISHED" and receipt.external_id == "mock-post-1"
    assert receipt.request_state["idempotency_key"]
    audit = db.scalar(select(Audit).where(Audit.action == "publication.published"))
    assert audit.details["action_source"] == "MANUAL_OWNER_CONFIRMED"
    confirmation = db.scalar(select(Audit).where(Audit.action == "publication.manual_confirmation"))
    assert confirmation.details["action_source"] == "MANUAL_OWNER_CONFIRMED"
    assert db.get(SystemSetting, "autopilot").value["paused"] == global_paused


def test_brand_pause_blocks_and_preserves_plan_for_safe_retry(setup):
    db, calls = setup
    data = ready(db)
    db.get(Brand, 1).paused = True
    db.commit()
    with pytest.raises(DomainError, match="Brand outward actions paused"):
        confirm(db, data)
    assert db.get(SystemSetting, "manual_publish_plan:" + data.plan_token)
    assert not db.scalar(select(Publication)) and not calls
    db.get(Brand, 1).paused = False
    db.commit()
    confirm(db, data)
    assert calls == [(1, 1)]


def test_autonomous_publish_stays_blocked_under_global_pause(setup):
    db, calls = setup
    with pytest.raises(DomainError, match="Outward actions paused"):
        service.publish(db, 1, 1)
    assert not db.scalar(select(Publication)) and not calls


def test_scheduled_job_stays_blocked_under_global_pause(setup):
    db, calls = setup
    job = Job(brand_id=1, kind="publish", target_id=1, key="publish:1:r1",
              payload={"revision": 1}, run_at=utcnow(), status="RUNNING")
    db.add(job)
    db.commit()
    with pytest.raises(DomainError, match="Outward actions paused"):
        execute(db, job)
    assert not db.scalar(select(Publication)) and not calls


def test_confirmation_cannot_bypass_approval_or_plan_expiry(setup):
    db, calls = setup
    data = ready(db)
    db.get(Content, 1).status = "REVIEW"
    db.commit()
    with pytest.raises(DomainError, match="approved"):
        confirm(db, data)
    db.get(Content, 1).status = "APPROVED"
    plan = db.get(SystemSetting, "manual_publish_plan:" + data.plan_token)
    plan.value = {**plan.value, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    db.commit()
    with pytest.raises(DomainError, match="expired"):
        confirm(db, data)
    assert not calls


def test_confirmation_cannot_bypass_changed_plan(setup):
    db, calls = setup
    data = ready(db)
    data.account_ids = {"facebook": 99}
    with pytest.raises(DomainError, match="expired or changed"):
        confirm(db, data)
    assert not calls and not db.scalar(select(Publication))


def test_confirmation_cannot_bypass_account_health(setup):
    db, calls = setup
    data = ready(db)
    account = db.get(PlatformAccount, 1)
    account.config = {**account.config, "token_status": "unhealthy"}
    db.commit()
    with pytest.raises(ProviderError, match="connection check"):
        confirm(db, data)
    assert not calls
    assert db.scalar(select(Publication)).state == "CONFIGURATION"


def test_duplicate_confirmation_never_double_publishes(setup):
    db, calls = setup
    data = ready(db)
    confirm(db, data)
    with pytest.raises(DomainError):
        confirm(db, data)
    assert calls == [(1, 1)]
    assert db.scalar(select(Publication)).state == "PUBLISHED"
