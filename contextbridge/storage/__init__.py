"""Storage package — factory for storage backends.

``get_store()`` is the single entry point used by the CLI and the web app.
It reads :class:`~contextbridge.config.Settings`, picks the backend, and (for
SQLite) imports any legacy JSON packages found in the storage directory the
first time they are seen.  The JSON files are left untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path

from contextbridge.config import Settings
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.storage.json_store import JSONStore
from contextbridge.storage.portable import dumps_package, export_package, import_package
from contextbridge.storage.sqlite_store import SQLiteStore
from contextbridge.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


def get_store(
    backend: str | None = None,
    *,
    settings: Settings | None = None,
    base_dir: str | Path | None = None,
    auto_migrate: bool = True,
) -> StorageBackend:
    """
    Instantiate a storage backend.

    Parameters
    ----------
    backend
        ``"sqlite"`` (default, recommended) or ``"json"``.  Falls back to
        ``settings.storage_backend`` / ``CB_STORAGE_BACKEND``.
    settings
        Explicit settings; built from the environment when omitted.
    base_dir
        Override the storage directory.
    auto_migrate
        When using SQLite, import legacy JSON packages found in *base_dir*.
    """
    settings = settings or Settings.from_env()
    chosen = (backend or settings.storage_backend).lower().strip()
    directory = Path(base_dir) if base_dir else settings.storage_dir

    if chosen == "json":
        return JSONStore(base_dir=directory)
    if chosen == "sqlite":
        store = SQLiteStore(base_dir=directory)
        if auto_migrate:
            import_legacy_json(directory, store)
        return store
    raise ValueError(f"Unknown storage backend '{chosen}'. Choose from: sqlite, json")


def import_legacy_json(base_dir: str | Path, target: StorageBackend) -> list[str]:
    """Copy JSON packages from *base_dir* into *target* if they are not there yet.

    Every stored version is imported so history survives.  Nothing is deleted.
    Returns the names that were imported.
    """
    base = Path(base_dir)
    if not base.exists():
        return []
    source = JSONStore(base_dir=base)
    imported: list[str] = []
    for name in source.list_packages():
        if target.exists(name):
            continue
        versions = source.list_versions(name)
        try:
            for version in versions:
                target.save(source.load(name, version=version))
            latest = source.load(name)
            if latest.version not in versions:
                target.save(latest)
            elif versions and latest.version != versions[-1]:
                # Rolled-back package: make sure the current version wins.
                target.save(latest)
            imported.append(name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not import legacy package '%s': %s", name, exc.__class__.__name__)
    if imported:
        logger.info(
            "Imported %d legacy JSON package(s) into %s", len(imported), target.backend_name
        )
    return imported


__all__ = [
    "get_store",
    "import_legacy_json",
    "JSONStore",
    "SQLiteStore",
    "VectorStore",
    "StorageBackend",
    "PackageNotFoundError",
    "export_package",
    "import_package",
    "dumps_package",
]
