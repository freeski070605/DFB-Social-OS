from pathlib import Path
from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DFB_", env_file=ROOT / ".env", extra="ignore")
    database_url: str = "sqlite:///./data/dfb.sqlite3"
    storage_root: Path = ROOT / "data/media"
    ollama_url: str = "http://127.0.0.1:11434"
    encryption_key: str = ""
    cookie_secure: bool = False
    public_origin: str = "https://dfb-social-os.vercel.app"
    allowed_origins: list[str] = ["http://127.0.0.1:8000", "http://localhost:8000", "http://localhost:5173", "http://127.0.0.1:5173", "https://dfb-social-os.vercel.app"]
    public_media_provider: str = "local_unavailable"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_public_base_url: str = ""
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_base_url: str = ""
    meta_api_version: str = "v23.0"
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_config_id: str = ""
    meta_redirect_uri: str = ""
    meta_verify_token: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = ""
    scheduler_enabled: bool = True
    session_hours: int = 12

    @field_validator("meta_api_version")
    @classmethod
    def api_version(cls, value):
        import re
        if not re.fullmatch(r"v\d+\.\d+", value):
            raise ValueError("Meta version must have form v23.0")
        return value


def public_callback_url(path: str, configured: str = "") -> str:
    cfg = settings()
    candidate = configured.strip() if isinstance(configured, str) else ""
    public = (cfg.public_origin or "").strip().rstrip("/")
    if candidate and not candidate.startswith(("http://127.0.0.1", "http://localhost", "http://0.0.0.0")):
        return candidate.rstrip("/")
    if public:
        return public.rstrip("/") + path
    return candidate.rstrip("/") if candidate else path


@lru_cache
def settings():
    return Settings()
