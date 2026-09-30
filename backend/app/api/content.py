import json
from typing import Literal
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from app.db.session import get_db
from app.security.auth import authenticated
from app.models import Brand, Content, Publication, SystemSetting
from app.repositories.common import require, serialize, list_brand
from app.schemas.domain import ContentInput, KnowledgeInput, GenerationInput, ScheduleInput
from app.content import service as content_service
from app.knowledge import service as knowledge_service
from app.creative.service import render_content
from app.scheduling.service import schedule
from app.content.director import enqueue
from app.publishing.service import publish, reconcile
from app.publishing.providers import ManualExportPublisher
from app.audit.service import record
from app.knowledge import bulk as knowledge_bulk
from app.knowledge import review as knowledge_review
from app.core.errors import DomainError, ProviderError
from app.publishing.service import account_for, publishing_blockers
from app.publishing.public_media import approved_asset, public_media_provider, r2_fingerprint
from app.publishing.providers import caption
import secrets
import httpx
from datetime import timedelta
from app.db.session import utcnow
from app.approvals.policy import outward_lock, brand_outward_allowed

router = APIRouter(prefix="/api/brands/{brand_id}", tags=["content"], dependencies=[Depends(authenticated)])


@router.get("/content")
def content_list(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, Content, brand_id, 1000)]


@router.get("/content/{key}")
def content_get(brand_id: int, key: int, db=Depends(get_db)):
    return serialize(require(db, Content, key, brand_id))


@router.post("/content")
@router.put("/content/{key}")
def content_save(brand_id: int, data: ContentInput, key: int | None = None, admin=Depends(authenticated), db=Depends(get_db)):
    item = content_service.save(db, brand_id, data, key, admin.username)
    db.commit()
    return serialize(item)


class TransitionInput(BaseModel):
    state: str
    reason: str = ""


@router.post("/content/{key}/transition")
def content_transition(brand_id: int, key: int, data: TransitionInput, admin=Depends(authenticated), db=Depends(get_db)):
    from app.core.errors import DomainError
    if data.state not in {"DRAFT", "REVIEW", "APPROVED", "ARCHIVED"}:
        raise DomainError("Use scheduling and publishing actions for this state")
    item = content_service.transition(db, require(db, Content, key, brand_id), data.state, admin.username, data.reason)
    db.commit()
    return serialize(item)


@router.post("/generate")
def generate(brand_id: int, data: GenerationInput, admin=Depends(authenticated), db=Depends(get_db)):
    data.pillar = content_service.canonical_pillar(db, brand_id, data.pillar)
    if not data.knowledge_refs:
        raise DomainError("Select and attach approved knowledge before generating")
    if not content_service.generation_candidates(db, brand_id, data.topic, data.pillar, data.knowledge_refs):
        raise DomainError("Approve and enable knowledge for this pillar before AI generation")
    payload = data.model_dump()
    if data.update_current:
        if not data.parent_id:
            raise DomainError("Save the draft before updating it with generated content")
        draft = require(db, Content, data.parent_id, brand_id)
        if draft.status != "DRAFT" or (draft.topic, draft.pillar, draft.format) != (data.topic, data.pillar, data.format):
            raise DomainError("Save and review the current draft before generating", 409)
        payload.update({"_editor_target_id": draft.id, "_expected_revision": draft.revision})
    job = enqueue(db, brand_id, "generate", target_id=data.parent_id if data.update_current else None, payload=payload)
    record(db, "generation.request", job.id, brand_id, admin.username)
    db.commit()
    return serialize(job)


@router.post("/director")
def director(brand_id: int, admin=Depends(authenticated), db=Depends(get_db)):
    job = enqueue(db, brand_id, "director")
    record(db, "director.request", job.id, brand_id, admin.username)
    db.commit()
    return serialize(job)


@router.post("/content/{key}/render")
def render(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    item = render_content(db, brand_id, key, admin.username)
    db.commit()
    return serialize(item)


@router.post("/content/{key}/schedule")
def schedule_content(brand_id: int, key: int, data: ScheduleInput, admin=Depends(authenticated), db=Depends(get_db)):
    job = schedule(db, brand_id, key, data.run_at, admin.username, data.override_window, data.reason)
    db.commit()
    return serialize(job)


@router.post("/content/{key}/publish")
def publish_content(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    return serialize(publish(db, brand_id, key, admin.username))


class DryRunInput(BaseModel):
    platforms: list[Literal["facebook", "instagram"]] = Field(min_length=1, max_length=2)
    account_ids: dict[str, int]


class ManualPublishInput(DryRunInput):
    revision: int
    exact_caption: str
    media_keys: list[str]
    plan_token: str
    confirmed: bool


@router.post("/content/{key}/manual-meta-publish")
def manual_meta_publish(brand_id: int, key: int, data: ManualPublishInput,
                        admin=Depends(authenticated), db=Depends(get_db)):
    with outward_lock:
        item = require(db, Content, key, brand_id)
        if item.status != "APPROVED":
            raise DomainError("Content must be approved before manual publishing", 409)
        if item.revision != data.revision or caption(item) != data.exact_caption or [a["key"] for a in item.assets] != data.media_keys:
            raise DomainError("Content changed since preview; review it again", 409)
        if len(set(data.platforms)) != len(data.platforms):
            raise DomainError("Select each platform once")
        if not data.confirmed or len(data.plan_token) != 32:
            raise DomainError("Confirm a current READY dry run before publishing", 409)
        plan_key = "manual_publish_plan:" + data.plan_token
        saved = db.get(SystemSetting, plan_key, populate_existing=True)
        expected = {"brand_id": brand_id, "content_id": key, "revision": data.revision,
                    "platforms": data.platforms, "account_ids": data.account_ids,
                    "exact_caption": data.exact_caption, "media_keys": data.media_keys}
        if not saved or saved.value.get("expires_at", "") < utcnow().isoformat() or any(saved.value.get(k) != v for k, v in expected.items()):
            raise DomainError("READY dry run expired or changed; run it again", 409)
        brand_outward_allowed(db, require(db, Brand, brand_id))
        for asset in item.assets:
            approved_asset(item, asset)
        db.delete(saved)
        record(db, "publication.manual_confirmation", item.id, brand_id, admin.username,
               details={"platforms": data.platforms, "action_source": "MANUAL_OWNER_CONFIRMED"})
        db.commit()  # One use once all local safety gates pass; publication intent is durable before Meta.
        return serialize(publish(db, brand_id, key, admin.username, data.platforms, data.account_ids,
                                 action_source="MANUAL_OWNER_CONFIRMED"))


@router.post("/content/{key}/publish-dry-run")
def publish_dry_run(brand_id: int, key: int, data: DryRunInput, admin=Depends(authenticated), db=Depends(get_db)):
    item = require(db, Content, key, brand_id)
    reasons = []
    if item.status != "APPROVED":
        reasons.append("Content must be approved and unchanged")
    if len(set(data.platforms)) != len(data.platforms):
        reasons.append("Select each platform once")
    accounts = {}
    for platform in data.platforms:
        try:
            account = account_for(db, brand_id, platform)
        except DomainError as exc:
            account = None
            reasons.append(exc.message)
        if not account or account.id != data.account_ids.get(platform):
            reasons.append("Explicitly select the active " + platform + " account")
        else:
            accounts[platform] = account
            reasons.extend(platform + ": " + reason for reason in publishing_blockers(item, account, platform))
    if item.status == "APPROVED":
        for asset in item.assets:
            try:
                approved_asset(item, asset)
            except ProviderError as exc:
                reasons.append(exc.message)
                break
    if reasons:
        return {"status": "BLOCKED", "reasons": reasons, "plan": []}

    plan = []
    # Persist cleanup intent before validation requests. No Graph publishing endpoint is called.
    cleanup_key = "dryrun_media:" + secrets.token_hex(16)
    cleanup = SystemSetting(key=cleanup_key, value={"objects": []})
    db.add(cleanup)
    db.commit()
    try:
        provider = public_media_provider() if item.assets else None
        urls = []
        for platform in data.platforms:
            for asset in item.assets:
                object_key, url = provider.prepare_object(item, asset, platform)
                cleanup.value = {"objects": [*cleanup.value["objects"], object_key]}
                db.commit()
                urls.append(url)
                try:
                    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
                        response = client.head(url)
                    if response.status_code != 200:
                        reasons.append("PUBLIC_FETCH_FAILED: Public media URL returned HTTP " + str(response.status_code))
                    elif response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
                            "image/jpeg" if platform == "instagram" or asset["key"].lower().endswith((".jpg", ".jpeg"))
                            else "image/png"):
                        reasons.append("PUBLIC_CONTENT_MISMATCH: Public media Content-Type does not match the image")
                except httpx.HTTPError:
                    reasons.append("PUBLIC_FETCH_FAILED: Public media URL accessibility check failed")
        for platform in data.platforms:
            plan.append({"platform": platform, "account_id": accounts[platform].id,
                         "account_name": (accounts[platform].config or {}).get("name", ""),
                         "action": "Instagram image/carousel" if platform == "instagram" else
                                   "Facebook Page feed with images" if urls else "Facebook Page text feed",
                         "caption": caption(item) if platform == "instagram" or urls else caption(item) or item.body,
                         "media_count": len(item.assets), "content_id": item.id, "revision": item.revision})
    except ProviderError as exc:
        reasons.append(exc.message)
    except Exception:
        reasons.append("R2 provider failed during media preparation")
    finally:
        if cleanup.value["objects"]:
            for object_key in list(cleanup.value["objects"]):
                try:
                    provider.delete(object_key)
                    cleanup.value = {"objects": [value for value in cleanup.value["objects"] if value != object_key]}
                    db.commit()
                except Exception:
                    reasons.append("CLEANUP_FAILED: Temporary media cleanup is pending retry")
        if not cleanup.value["objects"]:
            db.delete(cleanup)
            db.commit()
    record(db, "publication.dry_run", item.id, brand_id, admin.username,
           result="BLOCKED" if reasons else "READY", details={"platforms": data.platforms, "media_count": len(item.assets)})
    plan_token = None
    if not reasons:
        plan_token = secrets.token_hex(16)
        if item.assets:
            marker = db.get(SystemSetting, "r2_media_health")
            if marker is None:
                marker = SystemSetting(key="r2_media_health")
                db.add(marker)
            marker.value = {"fingerprint": r2_fingerprint(), "checked_at": utcnow().isoformat()}
        db.add(SystemSetting(key="manual_publish_plan:" + plan_token, value={
            "brand_id": brand_id, "content_id": key, "revision": item.revision,
            "platforms": data.platforms, "account_ids": data.account_ids,
            "exact_caption": caption(item), "media_keys": [a["key"] for a in item.assets],
            "expires_at": (utcnow() + timedelta(minutes=30)).isoformat()}))
    db.commit()
    return {"status": "BLOCKED" if reasons else "READY", "reasons": reasons, "plan": plan, "plan_token": plan_token}


@router.post("/content/{key}/export")
def export(brand_id: int, key: int, admin=Depends(authenticated), db=Depends(get_db)):
    item = require(db, Content, key, brand_id)
    if item.status not in {"APPROVED", "SCHEDULED", "PUBLISHED"}:
        from app.core.errors import DomainError
        raise DomainError("Approve content before exporting", 409)
    path = ManualExportPublisher().publish(item)
    record(db, "content.export", key, brand_id, admin.username)
    db.commit()
    return {"url": "/api/media/" + path}


@router.get("/publications")
def publications(brand_id: int, db=Depends(get_db)):
    return [serialize(x) for x in list_brand(db, Publication, brand_id)]


class ReconcileInput(BaseModel):
    external_id: str = ""
    confirmed_absent: bool = False
    reason: str


@router.post("/publications/{key}/reconcile")
def reconcile_publication(brand_id: int, key: int, data: ReconcileInput, admin=Depends(authenticated), db=Depends(get_db)):
    result = reconcile(db, brand_id, key, data.external_id, data.confirmed_absent, data.reason, admin.username)
    db.commit()
    return serialize(result)


@router.get("/knowledge")
def knowledge(brand_id: int, q: str = "", db=Depends(get_db)):
    return [serialize(k) for k in knowledge_service.search(db, brand_id, q)]


@router.get("/knowledge/candidates")
def knowledge_candidates(brand_id: int, topic: str = Query(default="", max_length=300),
                         pillar: str = Query(default="", max_length=120), db=Depends(get_db)):
    if not topic.strip() or not pillar.strip():
        return []
    return [serialize(item) for item in content_service.generation_candidates(db, brand_id, topic, pillar, limit=12)]


class ReviewFilters(BaseModel):
    q: str = Field(default="", max_length=200)
    verification: Literal["PENDING", "APPROVED", "REJECTED"] | None = None
    enabled: bool | None = None
    category: str = Field(default="", max_length=120)
    source: str = Field(default="", max_length=2000)
    source_contains: str = Field(default="", max_length=200)
    tag: str = Field(default="", max_length=120)
    safety: Literal["all", "sensitive", "standard"] = "all"
    sort: Literal["newest", "oldest", "title", "category", "source"] = "newest"


class ReviewSelectedId(BaseModel):
    id: int
    version: str = Field(min_length=64, max_length=64)


class ReviewSelectionInput(BaseModel):
    scope: Literal["ids", "matching"]
    ids: list[ReviewSelectedId] = Field(default_factory=list, max_length=20_000)
    filters: ReviewFilters = Field(default_factory=ReviewFilters)


class ReviewActionInput(BaseModel):
    selection_token: str = Field(min_length=20, max_length=100)
    action: Literal["APPROVE", "PENDING", "REJECT", "ENABLE", "DISABLE"]
    include_sensitive: bool = False


@router.get("/knowledge/review")
def knowledge_review_list(brand_id: int, q: str = "", verification: Literal["PENDING", "APPROVED", "REJECTED"] | None = None,
                          enabled: bool | None = None, category: str = "", source: str = "",
                          source_contains: str = "", tag: str = "", safety: Literal["all", "sensitive", "standard"] = "all",
                          sort: Literal["newest", "oldest", "title", "category", "source"] = "newest",
                          page: int = Query(default=1, ge=1), db=Depends(get_db)):
    filters = ReviewFilters(q=q, verification=verification, enabled=enabled, category=category, source=source,
                            source_contains=source_contains, tag=tag, safety=safety, sort=sort)
    return knowledge_review.list_page(db, brand_id, filters.model_dump(), page)


@router.post("/knowledge/review/selection")
def knowledge_review_selection(brand_id: int, data: ReviewSelectionInput, request: Request,
                               admin=Depends(authenticated), db=Depends(get_db)):
    return knowledge_review.create_selection(db, brand_id, admin, request.state.session.token_hash,
                                             data.scope, [item.model_dump() for item in data.ids],
                                             data.filters.model_dump())


@router.post("/knowledge/review/apply")
def knowledge_review_apply(brand_id: int, data: ReviewActionInput, request: Request,
                           admin=Depends(authenticated), db=Depends(get_db)):
    return knowledge_review.apply(db, brand_id, admin, request.state.session.token_hash,
                                  data.selection_token, data.action, data.include_sensitive)


class KnowledgeCommitInput(BaseModel):
    preview_token: str = Field(min_length=20, max_length=100)
    duplicate_mode: Literal["SKIP_DUPLICATES", "UPDATE_MATCHING"]
    skip_invalid: bool = False


@router.get("/knowledge/import/template.csv")
def knowledge_template():
    return Response(knowledge_bulk.csv_template(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="dfb-knowledge-template.csv"'})


@router.post("/knowledge/import/preview")
async def knowledge_import_preview(brand_id: int, request: Request, format: Literal["csv", "json"] = Form(...),
                                   file: UploadFile = File(...), admin=Depends(authenticated), db=Depends(get_db)):
    raw = await file.read(knowledge_bulk.MAX_BYTES + 1)
    if len(raw) > knowledge_bulk.MAX_BYTES:
        raise DomainError("Import exceeds 20 MB; split it into smaller files", 413)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DomainError("File must be UTF-8 encoded CSV or JSON") from exc
    return knowledge_bulk.preview(db, brand_id, content, format, admin.username,
                                  admin.id, request.state.session.token_hash, raw)


@router.post("/knowledge/import/commit")
def knowledge_import_commit(brand_id: int, data: KnowledgeCommitInput, request: Request,
                            admin=Depends(authenticated), db=Depends(get_db)):
    return knowledge_bulk.commit(db, brand_id, data.preview_token, data.duplicate_mode,
                                 data.skip_invalid, admin.username,
                                 admin.id, request.state.session.token_hash)


@router.get("/knowledge/export.{file_format}")
def knowledge_export(brand_id: int, file_format: str, db=Depends(get_db)):
    rows = knowledge_bulk.export_rows(db, brand_id)
    if file_format == "csv":
        content = knowledge_bulk.export_csv(rows)
        media_type = "text/csv; charset=utf-8"
    elif file_format == "json":
        content = json.dumps(rows, ensure_ascii=False, indent=2)
        media_type = "application/json; charset=utf-8"
    else:
        from app.core.errors import DomainError
        raise DomainError("Choose CSV or JSON for knowledge export")
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="dfb-brand-{brand_id}-knowledge.{file_format}"'})


@router.post("/knowledge")
@router.put("/knowledge/{key}")
def knowledge_save(brand_id: int, data: KnowledgeInput, key: int | None = None, admin=Depends(authenticated), db=Depends(get_db)):
    item = knowledge_service.save(db, brand_id, data, key, admin.username)
    db.commit()
    return serialize(item)
