import re
from sqlalchemy import select, or_, text
from app.models import Knowledge
from app.schemas.domain import KnowledgeInput
from app.repositories.common import require
from app.audit.service import record


def save(db, brand_id, data: KnowledgeInput, key=None, actor="admin"):
    obj = require(db, Knowledge, key, brand_id) if key else Knowledge(brand_id=brand_id)
    before = {"verification": obj.verification, "title": obj.title} if key else {}
    for field, value in data.model_dump().items():
        setattr(obj, field, value)
    db.add(obj)
    db.flush()
    record(db, "knowledge.update" if key else "knowledge.create", obj.id, brand_id, actor, before, data.model_dump())
    return obj


def search(db, brand_id, query="", approved=False, limit=100):
    stmt = select(Knowledge).where(Knowledge.brand_id == brand_id)
    if approved:
        stmt = stmt.where(Knowledge.verification == "APPROVED", Knowledge.enabled.is_(True))
    words = re.findall(r"\w{2,}", query.lower())[:12]
    if words and db.bind.dialect.name == "sqlite":
        ids = db.execute(text("SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH :q ORDER BY rank LIMIT 200"),
                         {"q": " OR ".join('"' + w + '"' for w in words)}).scalars().all()
        stmt = stmt.where(Knowledge.id.in_(ids))
    elif words:
        stmt = stmt.where(or_(*[Knowledge.body.ilike(f"%{w}%") | Knowledge.title.ilike(f"%{w}%") for w in words]))
    rows = list(db.scalars(stmt.limit(limit)))
    return sorted(rows, key=lambda k: sum((k.title.lower().count(w) * 3 + k.body.lower().count(w)) for w in words), reverse=True)
