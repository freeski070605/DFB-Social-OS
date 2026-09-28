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
    ollama_model: str = "qwen2.5:7b"
    encryption_key: str = ""
    cookie_secure: bool = False
    allowed_origins: list[str] = ["http://127.0.0.1:8000", "http://localhost:8000", "http://localhost:5173", "http://127.0.0.1:5173"]
    public_media_url: str = ""
    meta_api_version: str = "v23.0"
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    scheduler_enabled: bool = True
    session_hours: int = 12

    @field_validator("meta_api_version")
    @classmethod
    def api_version(cls, value):
        import re
        if not re.fullmatch(r"v\d+\.\d+", value):
            raise ValueError("Meta version must have form v23.0")
        return value


@lru_cache
def settings():
    return Settings()
