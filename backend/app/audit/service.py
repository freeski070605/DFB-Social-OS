import re
from app.models import Audit
from app.core.config import settings

PRIVATE = ("token", "secret", "password", "authorization", "access_key", "credential", "cookie", "csrf")


def safe(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(term in str(key).lower() for term in PRIVATE) else safe(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, str):
        for secret in (settings().encryption_key, settings().meta_app_secret, settings().meta_verify_token,
                       settings().s3_access_key, settings().s3_secret_key,
                       settings().r2_access_key_id, settings().r2_secret_access_key):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)(bearer\s+|access_token[=:]\s*|client_secret[=:]\s*)[^\s&,;]+", r"\1[REDACTED]", value)
    return value


def record(db, action, target="", brand_id=None, actor="admin", before=None, after=None, reason="", result="SUCCESS", details=None):
    db.add(Audit(action=action, target=str(target), brand_id=brand_id, actor=actor,
                 actor_type="ADMIN" if actor not in {"system", "ai", "platform"} else actor.upper(),
                 before=safe(before or {}), after=safe(after or {}), reason=safe(reason), result=result, details=safe(details or {})))
