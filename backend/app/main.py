from contextlib import asynccontextmanager
import logging
from filelock import FileLock, Timeout
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text, select
from sqlalchemy.exc import IntegrityError
from app.core.config import settings, ROOT
from app.core.errors import DomainError
from app.core.logging import configure
from app.db.session import SessionLocal, get_db
from app.models import Content
from app.brands.service import seed
from app.security.auth import authenticated
from app.storage.local import LocalStorage
from app.api import auth, brands, content, operations, system, webhooks


@asynccontextmanager
async def lifespan(app):
    configure()
    lock = FileLock(ROOT / "data/application.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise RuntimeError("DFB Social OS already running. Use one server process.")
    scheduler = None
    try:
        LocalStorage()
        if settings().encryption_key:
            from app.security.secrets import cipher
            cipher()
        with SessionLocal() as db:
            try:
                revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
                if not revision:
                    raise RuntimeError("Database has no migration version")
            except Exception as exc:
                raise RuntimeError("Run .venv/Scripts/alembic upgrade head before starting") from exc
            seed(db)
            db.commit()
        if settings().scheduler_enabled:
            from app.scheduling.worker import start
            scheduler = start()
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=True)
        lock.release()


app = FastAPI(title="DFB Social OS", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings().allowed_origins, allow_credentials=True,
                   allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in settings().allowed_origins and not request.url.path.startswith("/api/webhooks/"):
        return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.exception_handler(DomainError)
async def domain_error(request, exc):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({"detail": "Conflicting or invalid record. Refresh and try again."}, status_code=409)


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    logging.getLogger("dfb.api").exception("request_failed", exc_info=exc)
    return JSONResponse({"detail": "Unexpected server error. Inspect logs/dfb.jsonl."}, status_code=500)


for router in (auth.router, brands.router, content.router, operations.router, system.router, webhooks.router):
    app.include_router(router)


@app.get("/health")
def health(db=Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/media/{key:path}", dependencies=[Depends(authenticated)])
def media(key: str):
    path = LocalStorage().path(key)
    if not path.is_file():
        raise DomainError("Asset not found", 404)
    return FileResponse(path, filename=path.name if path.suffix == ".zip" else None)


@app.get("/media-public/{key:path}")
def public_media(key: str, db=Depends(get_db)):
    # Only a rendered original image referenced by content is public. No exports, previews or arbitrary files.
    if not key.endswith(".png") or "-preview" in key:
        raise DomainError("Asset not found", 404)
    parts = key.split("/")
    if len(parts) != 3 or not parts[1].isdigit():
        raise DomainError("Asset not found", 404)
    item = db.get(Content, int(parts[1]))
    if not item or not any(asset["key"] == key for asset in item.assets):
        raise DomainError("Asset not found", 404)
    path = LocalStorage().path(key)
    if not path.is_file():
        raise DomainError("Asset not found", 404)
    return FileResponse(path, media_type="image/png")


dist = ROOT / "frontend/dist"
if (dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/") or path.startswith("media-public/"):
        raise DomainError("Endpoint not found", 404)
    if not (dist / "index.html").exists():
        raise DomainError("Build the frontend with npm run build in frontend/", 503)
    return FileResponse(dist / "index.html")
