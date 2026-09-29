"""Brand-scoped, human-controlled knowledge review and bulk actions."""

import hashlib
import json
import secrets
from collections import Counter
from datetime import timedelta

from sqlalchemy import select, func

from app.audit.service import record
from app.core.errors import DomainError
from app.db.session import utcnow
from app.models import Knowledge, SystemSetting
from app.repositories.common import serialize

PAGE_SIZE = 30
MAX_SELECTION = 20_000
SENSITIVE_TERMS = {
    "Food safety": ("food safety", "foodborne", "food poisoning", "food handling", "cross contamination",
                    "raw meat", "safe temperature", "usda"),
    "Cleaning chemical safety": ("chemical", "bleach", "ammonia", "disinfectant", "pesticide", "epa"),
    "Electrical safety": ("electrical", "electricity", "wiring", "circuit", "outlet", "breaker"),
    "Gas safety": ("gas leak", "natural gas", "propane", "carbon monoxide"),
    "Home repair safety": ("home repair", "home maintenance", "diy", "ladder", "power tool", "structural"),
    "Financial guidance": ("budget", "financ", "credit", "debt", "loan", "tax", "bank", "cfpb"),
    "Employment or legal": ("employment", "career", "labor law", "legal", "contract", "workplace rights"),
    "Medical or health": ("medical", "health", "medication", "first aid", "symptom", "illness"),
}


def safety_reasons(category, source, tags, restrictions):
    text = " ".join([category or "", source or "", restrictions or "", *(tags or [])]).casefold()
    return [name for name, terms in SENSITIVE_TERMS.items() if any(term in text for term in terms)]


def version(item):
    fields = [item.title, item.category, item.body, item.source, item.restrictions,
              item.tags, item.verification, item.enabled,
              item.updated_at.isoformat() if item.updated_at else ""]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _metadata(db, brand_id, filters):
    stmt = select(Knowledge.id, Knowledge.title, Knowledge.category, Knowledge.source, Knowledge.tags,
                  Knowledge.restrictions, Knowledge.verification, Knowledge.enabled, Knowledge.updated_at)
    stmt = stmt.where(Knowledge.brand_id == brand_id)
    if filters.get("q"):
        query = filters["q"].strip()
        if query:
            stmt = stmt.where(Knowledge.title.icontains(query, autoescape=True) |
                              Knowledge.body.icontains(query, autoescape=True) |
                              Knowledge.source.icontains(query, autoescape=True))
    if filters.get("verification"):
        stmt = stmt.where(Knowledge.verification == filters["verification"])
    if filters.get("enabled") is not None:
        stmt = stmt.where(Knowledge.enabled.is_(filters["enabled"]))
    if filters.get("category"):
        stmt = stmt.where(Knowledge.category == filters["category"])
    if filters.get("source"):
        stmt = stmt.where(Knowledge.source == filters["source"])
    if filters.get("source_contains"):
        stmt = stmt.where(Knowledge.source.icontains(filters["source_contains"].strip(), autoescape=True))
    rows = list(db.execute(stmt).mappings())
    tag = filters.get("tag", "").strip().casefold()
    if tag:
        rows = [row for row in rows if any(tag in value.casefold() for value in (row["tags"] or []))]
    safety = filters.get("safety", "all")
    if safety != "all":
        rows = [row for row in rows if bool(safety_reasons(row["category"], row["source"], row["tags"],
                                                         row["restrictions"])) == (safety == "sensitive")]
    sort = filters.get("sort", "newest")
    if sort == "oldest":
        rows.sort(key=lambda row: (row["updated_at"] or utcnow(), row["id"]))
    elif sort in {"title", "category", "source"}:
        rows.sort(key=lambda row: ((row[sort] or "").casefold(), row["id"]))
    else:
        rows.sort(key=lambda row: (row["updated_at"] or utcnow(), row["id"]), reverse=True)
    return rows


def _load(db, brand_id, ids):
    found = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for item in db.scalars(select(Knowledge).where(Knowledge.brand_id == brand_id, Knowledge.id.in_(chunk))):
            found[item.id] = item
    return [found[key] for key in ids if key in found]


def list_page(db, brand_id, filters, page=1):
    matches = _metadata(db, brand_id, filters)
    ids = [row["id"] for row in matches[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
    items = _load(db, brand_id, ids)
    categories = db.execute(select(Knowledge.category, func.count()).where(Knowledge.brand_id == brand_id)
                            .group_by(Knowledge.category).order_by(Knowledge.category)).all()
    sources = db.execute(select(Knowledge.source, func.count()).where(Knowledge.brand_id == brand_id,
                         Knowledge.source != "").group_by(Knowledge.source)
                         .order_by(func.count().desc(), Knowledge.source).limit(200)).all()
    return {"total": len(matches), "page": page, "page_size": PAGE_SIZE,
            "rows": [{**serialize(item), "version": version(item),
                      "safety_reasons": safety_reasons(item.category, item.source, item.tags, item.restrictions)}
                     for item in items],
            "categories": [{"name": name, "count": count} for name, count in categories],
            "sources": [{"name": name, "count": count} for name, count in sources]}


def _key(token):
    return "knowledge_review:" + hashlib.sha256(token.encode()).hexdigest()


def create_selection(db, brand_id, admin, session_hash, scope, ids=None, filters=None):
    for old in db.scalars(select(SystemSetting).where(SystemSetting.key.like("knowledge_review:%"))):
        if old.value.get("expires_at", 0) <= utcnow().timestamp():
            db.delete(old)
    if scope == "matching":
        selected_ids = [row["id"] for row in _metadata(db, brand_id, filters or {})]
        supplied = None
    else:
        supplied = {row["id"]: row["version"] for row in (ids or [])}
        selected_ids = list(supplied)
        if len(selected_ids) != len(ids or []):
            raise DomainError("Selection contains repeated IDs")
    if not selected_ids or len(selected_ids) > MAX_SELECTION:
        raise DomainError("Select 1 to 20,000 knowledge records")
    items = _load(db, brand_id, selected_ids)
    if len(items) != len(selected_ids):
        raise DomainError("Selection contains records outside this brand or records that no longer exist", 409)
    versions = {str(item.id): version(item) for item in items}
    if supplied and any(versions[str(key)] != value for key, value in supplied.items()):
        raise DomainError("Knowledge changed after selection. Refresh and select it again.", 409)
    pillars = Counter(item.category for item in items)
    sources = Counter(item.source or "No source" for item in items)
    sensitive = sum(bool(safety_reasons(item.category, item.source, item.tags, item.restrictions)) for item in items)
    token = secrets.token_urlsafe(32)
    summary = {"count": len(items), "pillars": dict(pillars), "sources": dict(sources),
               "restrictions_count": sum(bool(item.restrictions.strip()) for item in items),
               "sensitive_count": sensitive}
    db.add(SystemSetting(key=_key(token), value={"brand_id": brand_id, "admin_id": admin.id,
           "session_hash": session_hash, "ids": selected_ids, "versions": versions,
           "expires_at": (utcnow() + timedelta(minutes=30)).timestamp()}))
    db.commit()
    return {"selection_token": token, **summary}


def apply(db, brand_id, admin, session_hash, token, action, include_sensitive=False):
    state = db.get(SystemSetting, _key(token))
    if not state or state.value.get("expires_at", 0) <= utcnow().timestamp():
        raise DomainError("Review selection expired or was already used. Select records again.", 409)
    saved = state.value
    if saved.get("brand_id") != brand_id or saved.get("admin_id") != admin.id or saved.get("session_hash") != session_hash:
        raise DomainError("Review selection belongs to another brand or session", 403)
    ids = saved["ids"]
    items = _load(db, brand_id, ids)
    if len(items) != len(ids) or any(version(item) != saved["versions"].get(str(item.id)) for item in items):
        raise DomainError("Knowledge changed after selection. Refresh and select it again.", 409)
    affected, skipped_sensitive = [], 0
    for item in items:
        if action == "APPROVE" and not include_sensitive and safety_reasons(item.category, item.source,
                                                                              item.tags, item.restrictions):
            skipped_sensitive += 1
            continue
        field = "enabled" if action in {"ENABLE", "DISABLE"} else "verification"
        value = {"APPROVE": "APPROVED", "PENDING": "PENDING", "REJECT": "REJECTED",
                 "ENABLE": True, "DISABLE": False}[action]
        if getattr(item, field) != value:
            setattr(item, field, value)
            affected.append(item.id)
    record(db, "knowledge.bulk_" + action.lower(), brand_id, brand_id, admin.username,
           details={"number_affected": len(affected), "record_ids": affected,
                    "skipped_sensitive": skipped_sensitive, "selected": len(ids)})
    db.delete(state)
    db.commit()
    return {"affected": len(affected), "record_ids": affected, "skipped_sensitive": skipped_sensitive,
            "selected": len(ids)}
