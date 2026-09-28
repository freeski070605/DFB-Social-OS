import json
import logging
from logging.handlers import RotatingFileHandler
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


def configure():
    (ROOT / "logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(ROOT / "logs/dfb.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("dfb")
    if not log.handlers:
        log.addHandler(handler)
    log.setLevel(logging.INFO)
