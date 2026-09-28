import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from sqlalchemy.engine import make_url
from app.core.config import settings, ROOT
from app.core.errors import DomainError
from app.db.session import utcnow


def backup(destination=None):
    url = make_url(settings().database_url)
    if url.get_backend_name() != "sqlite":
        raise DomainError("Use pg_dump for PostgreSQL backups")
    destination = Path(destination or ROOT / "data/backups")
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / ("dfb-" + utcnow().strftime("%Y%m%d-%H%M%S-%f") + ".zip")
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "dfb.sqlite3"
        with sqlite3.connect(url.database) as source, sqlite3.connect(database) as target:
            source.backup(target)
        manifest = {"version": 1, "created_at": utcnow().isoformat(), "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    "media": "Excluded; regenerate unpublished media from structured slides and templates", "secrets": "Encryption key is excluded; preserve .env separately"}
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.write(database, "dfb.sqlite3")
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for folder in (ROOT / "brands", ROOT / "assets/fonts"):
                for file in folder.rglob("*"):
                    if file.is_file():
                        archive.write(file, file.relative_to(ROOT).as_posix())
    return path


def restore(archive_path):
    url = make_url(settings().database_url)
    if url.get_backend_name() != "sqlite":
        raise DomainError("Use pg_restore for PostgreSQL")
    with ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        data = archive.read("dfb.sqlite3")
        if hashlib.sha256(data).hexdigest() != manifest["database_sha256"]:
            raise DomainError("Backup checksum failed")
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "restore.sqlite3"
            candidate.write_bytes(data)
            with sqlite3.connect(candidate) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DomainError("Backup database failed integrity validation")
                source.execute("UPDATE system_settings SET value = ? WHERE key = 'autopilot'", (json.dumps({"paused": True}),))
                source.execute("DELETE FROM admin_sessions")
                source.execute("UPDATE brands SET paused = 1")
                source.commit()
                with sqlite3.connect(url.database) as target:
                    source.backup(target)
    return Path(url.database)
