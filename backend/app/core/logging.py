import json
import logging
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit
from app.core.config import ROOT
from app.db.session import utcnow


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {"timestamp": utcnow().isoformat(), "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            result["exception"] = record.exc_info[0].__name__
        if hasattr(record, "job_id"):
            result["job_id"] = record.job_id
        return json.dumps(result)


class MetaCallbackAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access record includes the full path and query as argument 3.
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and urlsplit(path).path in {"/api/meta/callback", "/api/youtube/callback"}:
                args = list(record.args)
                args[2] = urlsplit(path).path
                record.args = tuple(args)
        return True


def configure():
    (ROOT / "logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(ROOT / "logs/dfb.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("dfb")
    if not log.handlers:
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, MetaCallbackAccessFilter) for item in access.filters):
        access.addFilter(MetaCallbackAccessFilter())
