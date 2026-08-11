"""Application configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.exceptions import ConfigurationError


ROOT_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT_DIR
DATA_DIR = RUNTIME_DIR / "data"
EXPORTS_DIR = DATA_DIR / "exports"
METADATA_DIR = DATA_DIR / "metadata"
QUEUE_DIR = RUNTIME_DIR / "queue"
QUEUE_IMPORTS_DIR = QUEUE_DIR / "imports"
CURRENT_EXPORT_PATH = QUEUE_DIR / "current_export.json"
DEFAULT_POSTGRES_DB_NAME = "crm_inventory"


def load_dotenv(path: Path | None = None) -> None:
    """Load simple KEY=VALUE pairs from .env without overriding existing env."""

    env_path = path or RUNTIME_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_list(name: str) -> tuple[str, ...]:
    value = (os.getenv(name) or "").strip()
    if not value:
        return ()
    if value.startswith("["):
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"{name} must be a JSON list or comma separated list") from exc
        if not isinstance(parsed, list):
            raise ConfigurationError(f"{name} must be a JSON list or comma separated list")
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    return tuple(item.strip() for item in value.replace(";", ",").split(",") if item.strip())


def _env_runtime_command_path(name: str, default: str) -> str:
    value = (os.getenv(name) or default).strip()
    if not value or value == default:
        return value
    path = Path(value)
    if path.is_absolute():
        return str(path)
    if "\\" in value or "/" in value or value.lower().endswith(".exe"):
        return str(RUNTIME_DIR / path)
    return value


def _env_database_url() -> str:
    explicit = (os.getenv("DATABASE_URL") or "").strip()
    if explicit:
        return explicit
    db_name = (os.getenv("DB_NAME") or DEFAULT_POSTGRES_DB_NAME).strip()
    db_user = (os.getenv("DB_USER") or "postgres").strip()
    db_password = os.getenv("DB_PASSWORD")
    db_host = (os.getenv("DB_HOST") or "localhost").strip()
    db_port = (os.getenv("DB_PORT") or "5432").strip()
    auth = quote_plus(db_user)
    if db_password:
        auth += f":{quote_plus(db_password)}"
    return f"postgresql+psycopg://{auth}@{db_host}:{db_port}/{quote_plus(db_name)}"


class Settings(BaseModel):
    database_url: str = Field(default=f"postgresql+psycopg://postgres:postgres@localhost:5432/{DEFAULT_POSTGRES_DB_NAME}")
    crm_export_source_name: str | None = Field(default=None)
    crm_export_source_id: str | None = Field(default=None)
    market_import_token: str | None = Field(default=None)
    my_id: str | None = Field(default=None)
    total_users: tuple[str, ...] = Field(default=())
    croc_path: str = Field(default="croc")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        if not (value.startswith("sqlite:///") or value.startswith("postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql+psycopg://")
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite:///")

    @property
    def database_path(self) -> Path:
        if not self.is_sqlite:
            raise ConfigurationError("database_path is only available for SQLite DATABASE_URL")
        raw_path = self.database_url.removeprefix("sqlite:///")
        path = Path(raw_path)
        if not path.is_absolute():
            path = RUNTIME_DIR / path
        return path

    @property
    def database_label(self) -> str:
        return str(self.database_path) if self.is_sqlite else self.database_url


def get_settings() -> Settings:
    load_dotenv()
    try:
        return Settings(
            database_url=_env_database_url(),
            crm_export_source_name=os.getenv("CRM_EXPORT_SOURCE_NAME"),
            crm_export_source_id=os.getenv("CRM_EXPORT_SOURCE_ID"),
            market_import_token=os.getenv("MARKET_IMPORT_TOKEN") or None,
            my_id=os.getenv("MY_ID"),
            total_users=_env_list("TOTAL_USERS"),
            croc_path=_env_runtime_command_path("CROC_PATH", "croc"),
        )
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc


def ensure_data_dirs() -> None:
    for path in (DATA_DIR, EXPORTS_DIR, METADATA_DIR, QUEUE_DIR, QUEUE_IMPORTS_DIR):
        path.mkdir(parents=True, exist_ok=True)
