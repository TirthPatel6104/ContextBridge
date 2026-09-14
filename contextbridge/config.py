"""Runtime configuration.

Settings are read from environment variables once, into an immutable
:class:`Settings` object that is passed explicitly to the pieces that need it
(storage factory, web app factory).  Nothing here reads secrets: API keys are
resolved lazily by the adapters and never stored on the settings object.
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

StorageBackendName = Literal["sqlite", "json"]


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


class Settings(BaseModel, frozen=True):
    """Process-wide configuration, built from the environment."""

    storage_dir: Path = Field(default=DEFAULT_STORAGE_DIR)
    storage_backend: StorageBackendName = "sqlite"
    default_adapter: str = "local"
    redact_by_default: bool = True
    max_upload_bytes: int = 25 * 1024 * 1024
    max_text_chars: int = 2_000_000
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    host: str = "127.0.0.1"
    port: int = 5000

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        """Build settings from ``CB_*`` environment variables."""
        storage_dir = os.getenv("CB_STORAGE_DIR") or str(DEFAULT_STORAGE_DIR)
        backend_raw = (os.getenv("CB_STORAGE_BACKEND") or "sqlite").strip().lower()
        backend: StorageBackendName = "json" if backend_raw == "json" else "sqlite"
        origins_raw = os.getenv("CB_ALLOWED_ORIGINS")
        origins = (
            tuple(o.strip() for o in origins_raw.split(",") if o.strip())
            if origins_raw
            else DEFAULT_ALLOWED_ORIGINS
        )
        values = {
            "storage_dir": Path(storage_dir).expanduser(),
            "storage_backend": backend,
            "default_adapter": (os.getenv("CB_DEFAULT_ADAPTER") or "local").strip().lower(),
            "redact_by_default": _env_bool("CB_REDACT", True),
            "max_upload_bytes": _env_int("CB_MAX_UPLOAD_MB", 25) * 1024 * 1024,
            "max_text_chars": _env_int("CB_MAX_TEXT_CHARS", 2_000_000),
            "allowed_origins": origins,
            "host": os.getenv("CB_HOST") or "127.0.0.1",
            "port": _env_int("CB_PORT", 5000),
        }
        values.update(overrides)
        return cls(**values)
