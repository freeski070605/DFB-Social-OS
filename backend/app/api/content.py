import json
from typing import Literal
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from app.db.session import get_db
from app.security.auth import authenticated
from app.models import Content, Publication
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
from app.core.errors import DomainError

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
    if not content_service.generation_candidates(db, brand_id, data.topic, data.pillar, data.knowledge_refs):
        raise DomainError("Approve and enable knowledge for this pillar before AI generation")
    job = enqueue(db, brand_id, "generate", payload=data.model_dump())
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
    return [serialize(item) for item in content_service.generation_candidates(db, brand_id, topic, pillar)]


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
