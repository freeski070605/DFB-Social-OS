from app.models import Audit


def record(db, action, target="", brand_id=None, actor="admin", before=None, after=None, reason="", result="SUCCESS", details=None):
    db.add(Audit(action=action, target=str(target), brand_id=brand_id, actor=actor,
                 actor_type="ADMIN" if actor not in {"system", "ai", "platform"} else actor.upper(),
                 before=before or {}, after=after or {}, reason=reason, result=result, details=details or {}))
