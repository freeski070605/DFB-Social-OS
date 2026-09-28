"""Brand-scoped, previewed bulk knowledge transfer."""
import csv
import hashlib
import io
import json
import re
import secrets
import unicodedata
from datetime import timedelta
from threading import RLock

from sqlalchemy import select

from app.audit.service import record
from app.core.errors import DomainError
from app.db.session import utcnow
from app.models import Brand, Knowledge, SystemSetting
from app.repositories.common import require
from app.schemas.domain import BrandConfig

COLUMNS = ("title", "category", "body", "source", "usage_restrictions", "tags", "verification",
           "available_for_retrieval")
MAX_BYTES = 20_000_000
MAX_RECORDS = 20_000
FLUSH_BATCH = 500
_commit_lock = RLock()


def normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def body_hash(value):
    return hashlib.sha256(normalized(value).encode("utf-8")).hexdigest()


def _parse(content, file_format):
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise DomainError("Import exceeds 20 MB; split it into smaller files", 413)
    if file_format == "csv":
        try:
            reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""), strict=True)
            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)) or set(headers) != set(COLUMNS):
                raise DomainError("CSV headers must match the downloadable template exactly")
            rows = []
            for row in reader:
                if len(rows) >= MAX_RECORDS:
                    raise DomainError("Import exceeds 20,000 records; split it into smaller files", 413)
                rows.append(row)
            return rows
        except csv.Error:
            raise DomainError("Malformed CSV. Check quoting and line breaks before retrying")
    if file_format == "json":
        try:
            rows = json.loads(content.lstrip("\ufeff"))
        except ValueError:
            raise DomainError("Malformed JSON. Upload an array of knowledge objects")
        if not isinstance(rows, list):
            raise DomainError("JSON import must be an array of knowledge objects")
        if len(rows) > MAX_RECORDS:
            raise DomainError("Import exceeds 20,000 records; split it into smaller files", 413)
        return rows
    raise DomainError("Choose CSV or JSON")


def _tags(value, file_format, errors):
    if file_format == "csv":
        if not isinstance(value, str):
            errors.append("Tags must be text")
            return []
        if value.lstrip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                errors.append("Tags JSON array is malformed")
                return []
        else:
            value = value.split(",")
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        errors.append("Tags must be an array of strings or comma-separated CSV text")
        return []
    tags = [tag.strip() for tag in value if tag.strip()]
    if len(tags) > 30 or any(len(tag) > 120 for tag in tags):
        errors.append("Use at most 30 tags of 120 characters each")
    return list(dict.fromkeys(tags))


def _bool(value, file_format, errors):
    if isinstance(value, bool):
        return value
    if file_format == "csv" and isinstance(value, str):
        if value.strip().lower() in {"true", "yes", "1"}:
            return True
        if value.strip().lower() in {"false", "no", "0"}:
            return False
    errors.append("Available for retrieval must be a boolean (true or false)")
    return False


def _clean(raw, file_format, categories):
    errors, warnings = [], []
    if not isinstance(raw, dict):
        return None, ["Record must be an object"], warnings
    extra = set(raw) - set(COLUMNS)
    if extra:
        errors.append("Unsupported fields: " + ", ".join(sorted(map(str, extra))))
    def string(field, maximum, *, required=False, preserve=False):
        value = raw.get(field, "")
        if not isinstance(value, str):
            errors.append(f"{field} must be text")
            return ""
        if required and not value.strip():
            errors.append(f"{field} is required")
        if len(value) > maximum:
            errors.append(f"{field} exceeds {maximum} characters")
        return value if preserve else value.strip()

    title = string("title", 300, required=True)
    category = string("category", 120, required=True)
    body = string("body", 50_000, required=True, preserve=True)
    source = string("source", 2_000, preserve=True)
    restrictions = string("usage_restrictions", 2_000, preserve=True)
    canonical = categories.get(normalized(category))
    if category and not canonical and categories:
        errors.append("category must match a pillar or an existing category for this brand")
    elif canonical:
        category = canonical
    verification = raw.get("verification", "")
    if not isinstance(verification, str) or verification.strip().upper() not in {"PENDING", "APPROVED", "REJECTED"}:
        verification = "PENDING"
        warnings.append("Verification missing or unrecognized; set to PENDING")
    else:
        verification = verification.strip().upper()
    enabled = _bool(raw.get("available_for_retrieval"), file_format, errors)
    tags = _tags(raw.get("tags", "" if file_format == "csv" else []), file_format, errors)
    if not source:
        warnings.append("No source/reference supplied")
    if enabled and verification != "APPROVED":
        warnings.append("Not eligible for AI retrieval until verification is APPROVED")
    return {"title": title, "category": category, "body": body, "source": source,
            "restrictions": restrictions, "tags": tags, "verification": verification, "enabled": enabled}, errors, warnings


def _snapshot(db, brand):
    existing = db.scalars(select(Knowledge).where(Knowledge.brand_id == brand.id).order_by(Knowledge.id)).all()
    pillars = BrandConfig.model_validate(brand.config).pillars
    categories = {normalized(name): name for name in pillars if name.strip()}
    for item in existing:
        categories.setdefault(normalized(item.category), item.category)
    state = {"pillars": pillars, "records": [(item.id, normalized(item.title), body_hash(item.body),
             item.updated_at.isoformat() if item.updated_at else "") for item in existing]}
    fingerprint = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return existing, categories, fingerprint


def plan(db, brand_id, content, file_format):
    brand = require(db, Brand, brand_id)
    existing, categories, fingerprint = _snapshot(db, brand)
    raw_rows = _parse(content, file_format)
    by_title, by_body = {}, {}
    for item in existing:
        by_title.setdefault(normalized(item.title), set()).add(item.id)
        by_body.setdefault(body_hash(item.body), set()).add(item.id)
    seen_title, seen_body, seen_targets = {}, {}, {}
    rows = []
    for number, raw in enumerate(raw_rows, start=2 if file_format == "csv" else 1):
        data, errors, warnings = _clean(raw, file_format, categories)
        match_id, duplicate_of = None, None
        if data and not errors:
            title_key, body_key = normalized(data["title"]), body_hash(data["body"])
            matches = by_title.get(title_key, set()) | by_body.get(body_key, set())
            if len(matches) > 1:
                errors.append("Title and body match different existing records; resolve manually")
            elif matches:
                match_id = next(iter(matches))
                if match_id in seen_targets:
                    duplicate_of = seen_targets[match_id]
                    warnings.append(f"Duplicate of import row {duplicate_of}; later row will be skipped")
                else:
                    seen_targets[match_id] = number
                    warnings.append(f"Matches existing knowledge #{match_id}")
            else:
                duplicate_of = seen_title.get(title_key) or seen_body.get(body_key)
                if duplicate_of:
                    warnings.append(f"Duplicate of import row {duplicate_of}; later row will be skipped")
            if not duplicate_of and not errors:
                seen_title[title_key] = number
                seen_body[body_key] = number
        rows.append({"row": number, "status": "INVALID" if errors else "DUPLICATE" if match_id or duplicate_of else "VALID",
                     "matching_id": match_id, "duplicate_of_row": duplicate_of, "errors": errors, "warnings": warnings,
                     "record": {"title": data["title"], "category": data["category"], "body": data["body"],
                                "source": data["source"], "usage_restrictions": data["restrictions"],
                                "tags": data["tags"], "verification": data["verification"],
                                "available_for_retrieval": data["enabled"]} if data else None,
                     "_data": data})
    valid = sum(not row["errors"] for row in rows)
    invalid = len(rows) - valid
    duplicates = sum(row["status"] == "DUPLICATE" for row in rows)
    warnings = sum(bool(row["warnings"]) for row in rows)
    return {"total": len(rows), "valid": valid, "invalid": invalid, "duplicates": duplicates,
            "warnings": warnings, "rows": rows, "fingerprint": fingerprint,
            "_existing": {item.id: item for item in existing}}


def _public(plan_result):
    return {key: plan_result[key] for key in ("total", "valid", "invalid", "duplicates", "warnings")} | {
        "rows": [{key: value for key, value in row.items() if key != "_data"} for row in plan_result["rows"]]}


def _key(token):
    return "knowledge_import:" + hashlib.sha256(token.encode()).hexdigest()[:60]


def preview(db, brand_id, content, file_format, actor, admin_id, session_hash):
    require(db, Brand, brand_id)
    for old in db.scalars(select(SystemSetting).where(SystemSetting.key.like("knowledge_import:%"))):
        if old.value.get("expires_at", 0) < utcnow().timestamp() or (old.value.get("brand_id") == brand_id and old.value.get("session_hash") == session_hash):
            db.delete(old)
    record(db, "knowledge.import_started", brand_id, brand_id, actor, details={"format": file_format})
    try:
        result = plan(db, brand_id, content, file_format)
    except DomainError as exc:
        record(db, "knowledge.import_previewed", brand_id, brand_id, actor, result="REJECTED",
               details={"error": exc.message, "format": file_format})
        db.commit()
        return {"file_errors": [exc.message], "total": 0, "valid": 0, "invalid": 0,
                "duplicates": 0, "warnings": 0, "rows": [], "preview_token": None}
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256((file_format + "\0" + content).encode("utf-8")).hexdigest()
    db.add(SystemSetting(key=_key(token), value={"brand_id": brand_id, "admin_id": admin_id,
        "session_hash": session_hash, "content_digest": digest, "fingerprint": result["fingerprint"],
        "expires_at": (utcnow() + timedelta(minutes=30)).timestamp()}))
    record(db, "knowledge.import_previewed", brand_id, brand_id, actor,
           details={"format": file_format, "total": result["total"], "valid": result["valid"],
                    "invalid": result["invalid"], "duplicates": result["duplicates"], "warnings": result["warnings"]})
    db.commit()
    return {**_public(result), "file_errors": [], "preview_token": token}


def commit(db, brand_id, content, file_format, preview_token, duplicate_mode, skip_invalid,
           actor, admin_id, session_hash):
    with _commit_lock:
        return _commit(db, brand_id, content, file_format, preview_token, duplicate_mode,
                       skip_invalid, actor, admin_id, session_hash)


def _commit(db, brand_id, content, file_format, preview_token, duplicate_mode, skip_invalid,
            actor, admin_id, session_hash):
    if duplicate_mode not in {"SKIP_DUPLICATES", "UPDATE_MATCHING"}:
        raise DomainError("Choose Skip duplicates or Update matching")
    pending = db.get(SystemSetting, _key(preview_token)) if preview_token else None
    if not pending or pending.value.get("brand_id") != brand_id or pending.value.get("admin_id") != admin_id or pending.value.get("session_hash") != session_hash or pending.value.get("expires_at", 0) < utcnow().timestamp():
        raise DomainError("Import preview expired or belongs to another session/brand. Preview the file again.", 409)
    digest = hashlib.sha256((file_format + "\0" + content).encode("utf-8")).hexdigest()
    if digest != pending.value["content_digest"]:
        raise DomainError("Import file changed after preview. Preview it again.", 409)
    result = plan(db, brand_id, content, file_format)
    if result["fingerprint"] != pending.value["fingerprint"]:
        raise DomainError("Brand knowledge or pillars changed after preview. Preview the file again.", 409)
    if result["invalid"] and not skip_invalid:
        raise DomainError("Review invalid rows and explicitly choose to skip them before committing", 409)
    created = updated = skipped = rejected = 0
    for row in result["rows"]:
        if row["errors"]:
            rejected += 1
            continue
        if row["duplicate_of_row"]:
            skipped += 1
            continue
        if row["matching_id"]:
            if duplicate_mode == "SKIP_DUPLICATES":
                skipped += 1
                continue
            item = result["_existing"][row["matching_id"]]
            updated += 1
        else:
            item = Knowledge(brand_id=brand_id)
            db.add(item)
            created += 1
        for field, value in row["_data"].items():
            setattr(item, field, value)
        if (created + updated) % FLUSH_BATCH == 0:
            db.flush()
    db.delete(pending)
    record(db, "knowledge.import_committed", brand_id, brand_id, actor,
           details={"format": file_format, "mode": duplicate_mode, "total": result["total"],
                    "created": created, "updated": updated, "skipped": skipped, "rejected": rejected})
    db.commit()
    return {"total": result["total"], "created": created, "updated": updated,
            "skipped": skipped, "rejected": rejected}


def export_rows(db, brand_id):
    require(db, Brand, brand_id)
    rows = db.scalars(select(Knowledge).where(Knowledge.brand_id == brand_id).order_by(Knowledge.id)).all()
    return [{"title": item.title, "category": item.category, "body": item.body, "source": item.source,
             "usage_restrictions": item.restrictions, "tags": list(item.tags or []),
             "verification": item.verification, "available_for_retrieval": item.enabled} for item in rows]


def export_csv(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "tags": json.dumps(row["tags"], ensure_ascii=False),
                         "available_for_retrieval": "true" if row["available_for_retrieval"] else "false"})
    return "\ufeff" + output.getvalue()


def csv_template():
    return export_csv([])
