import json
from typing import Literal
from fastapi import APIRouter, Depends, Request
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


class KnowledgeImportInput(BaseModel):
    format: Literal["csv", "json"]
    content: str = Field(max_length=20_000_000)


class KnowledgeCommitInput(KnowledgeImportInput):
    preview_token: str = Field(min_length=20, max_length=100)
    duplicate_mode: Literal["SKIP_DUPLICATES", "UPDATE_MATCHING"]
    skip_invalid: bool = False


@router.get("/knowledge/import/template.csv")
def knowledge_template():
    return Response(knowledge_bulk.csv_template(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="dfb-knowledge-template.csv"'})


@router.post("/knowledge/import/preview")
def knowledge_import_preview(brand_id: int, data: KnowledgeImportInput, request: Request,
                             admin=Depends(authenticated), db=Depends(get_db)):
    return knowledge_bulk.preview(db, brand_id, data.content, data.format, admin.username,
                                  admin.id, request.state.session.token_hash)


@router.post("/knowledge/import/commit")
def knowledge_import_commit(brand_id: int, data: KnowledgeCommitInput, request: Request,
                            admin=Depends(authenticated), db=Depends(get_db)):
    return knowledge_bulk.commit(db, brand_id, data.content, data.format, data.preview_token,
                                 data.duplicate_mode, data.skip_invalid, admin.username,
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
