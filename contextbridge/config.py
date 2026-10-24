"""Runtime configuration.

Settings are read from environment variables once, into an immutable
:class:`Settings` object that is passed explicitly to the pieces that need it
(storage factory, web app factory).  Nothing here reads secrets: API keys are
resolved lazily by the adapters and never stored on the settings object.  The
only exception is ``api_key`` (``CB_API_KEY``), the shared secret that guards
the HTTP API when it is deployed beyond loopback; it is compared, never logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_STORAGE_DIR = Path.home() / ".contextbridge"

#: Origins allowed to call the local API from a browser.  The Chrome extension
#: runs as a content script, so its requests carry the chat site's origin.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://claude.ai",
)

StorageBackendName = Literal["sqlite", "json", "postgres"]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


class Settings(BaseModel, frozen=True):
    """Process-wide configuration, built from the environment."""

    storage_dir: Path = Field(default=DEFAULT_STORAGE_DIR)
    storage_backend: StorageBackendName = "sqlite"
    database_url: str = Field(
        default="",
        description="PostgreSQL DSN used when storage_backend is 'postgres' (CB_DATABASE_URL)",
    )
    default_adapter: str = "local"
    redact_by_default: bool = True
    max_upload_bytes: int = 25 * 1024 * 1024
    max_text_chars: int = 2_000_000
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    host: str = "127.0.0.1"
    port: int = 5000
    api_key: str = Field(default="", description="Shared secret for the HTTP API (CB_API_KEY)")
    environment: str = Field(
        default="development", description="development | staging | production"
    )
    otel_enabled: bool = Field(default=False, description="Export OpenTelemetry traces")
    otel_service_name: str = "contextbridge"
    otel_console: bool = Field(default=False, description="Also print spans to stdout (debug)")

    @property
    def api_key_required(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        """Build settings from ``CB_*`` environment variables."""
        storage_dir = os.getenv("CB_STORAGE_DIR") or str(DEFAULT_STORAGE_DIR)
        backend_raw = (os.getenv("CB_STORAGE_BACKEND") or "").strip().lower()
        database_url = _env_str("CB_DATABASE_URL") or _env_str("DATABASE_URL")
        if backend_raw in {"json", "postgres", "postgresql", "pg"}:
            backend: StorageBackendName = "json" if backend_raw == "json" else "postgres"
        elif backend_raw == "":
            # A DSN without an explicit backend means "use Postgres".
            backend = "postgres" if database_url else "sqlite"
        else:
            backend = "sqlite"
        origins_raw = os.getenv("CB_ALLOWED_ORIGINS")
        origins = (
            tuple(o.strip() for o in origins_raw.split(",") if o.strip())
            if origins_raw
            else DEFAULT_ALLOWED_ORIGINS
        )
        otel_endpoint = _env_str("OTEL_EXPORTER_OTLP_ENDPOINT") or _env_str(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
        )
        values = {
            "storage_dir": Path(storage_dir).expanduser(),
            "storage_backend": backend,
            "database_url": database_url,
            "default_adapter": (os.getenv("CB_DEFAULT_ADAPTER") or "local").strip().lower(),
            "redact_by_default": _env_bool("CB_REDACT", True),
            "max_upload_bytes": _env_int("CB_MAX_UPLOAD_MB", 25) * 1024 * 1024,
            "max_text_chars": _env_int("CB_MAX_TEXT_CHARS", 2_000_000),
            "allowed_origins": origins,
            "host": os.getenv("CB_HOST") or "127.0.0.1",
            "port": _env_int("CB_PORT", 5000),
            "api_key": _env_str("CB_API_KEY"),
            "environment": _env_str("CB_ENVIRONMENT", "development").lower(),
            "otel_enabled": _env_bool("CB_OTEL_ENABLED", bool(otel_endpoint)),
            "otel_service_name": _env_str("OTEL_SERVICE_NAME", "contextbridge"),
            "otel_console": _env_bool("CB_OTEL_CONSOLE", False),
        }
        values.update(overrides)
        return cls(**values)
