"""SQLite storage backend — the recommended durable store.

Design
------
* One database file (``contextbridge.db``) inside the storage directory.
* ``packages`` holds one summary row per package (name, current version,
  counts, timestamps) so listings never deserialise full payloads.
* ``package_versions`` holds the complete JSON payload of every saved
  version, which keeps the on-disk representation identical to the portable
  JSON format and makes rollback / export trivial.
* Writes are wrapped in transactions and guarded by a lock so the Flask dev
  server's threads cannot interleave partial writes.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contextbridge.models import ContextPackage
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.validation import is_valid_package_name, validate_package_name

logger = logging.getLogger(__name__)

DB_FILENAME = "contextbridge.db"
USER_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    name           TEXT PRIMARY KEY,
    version        INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    source_model   TEXT NOT NULL DEFAULT '',
    item_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_versions (
    name     TEXT NOT NULL,
    version  INTEGER NOT NULL,
    payload  TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (name, version),
    FOREIGN KEY (name) REFERENCES packages(name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_packages_updated ON packages(updated_at);
"""


class SQLiteStore(StorageBackend):
    """SQLite-backed :class:`StorageBackend`."""

    backend_name = "sqlite"

    def __init__(self, base_dir: str | Path | None = None, *, db_path: str | Path | None = None):
        if db_path is not None:
            self._db_path = Path(db_path)
            self._base = self._db_path.parent
        else:
            self._base = Path(base_dir) if base_dir else Path.home() / ".contextbridge"
            self._db_path = self._base / DB_FILENAME
        self._base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # explicit transactions
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- Setup ---------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current < USER_VERSION:
                self._conn.execute(f"PRAGMA user_version={USER_VERSION}")

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def db_path(self) -> Path:
        return self._db_path

    def location(self) -> str:
        return str(self._db_path)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:  # already closed
                pass

    # -- StorageBackend interface -------------------------------------------

    def save(self, package: ContextPackage) -> None:
        name = validate_package_name(package.name)
        payload = package.model_dump_json()
        now = datetime.now(UTC).isoformat()
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.execute(
                    """
                    INSERT INTO packages
                        (name, version, schema_version, source_model, item_count,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        version = excluded.version,
                        schema_version = excluded.schema_version,
                        source_model = excluded.source_model,
                        item_count = excluded.item_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        name,
                        package.version,
                        package.schema_version,
                        package.source_model,
                        package.memory.total_items,
                        package.created_at.isoformat(),
                        package.updated_at.isoformat(),
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO package_versions (name, version, payload, saved_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name, version) DO UPDATE SET
                        payload = excluded.payload,
                        saved_at = excluded.saved_at
                    """,
                    (name, package.version, payload, now),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        logger.info("Saved package '%s' v%d (sqlite)", name, package.version)

    def load(self, name: str, *, version: int | None = None) -> ContextPackage:
        name = validate_package_name(name)
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT version FROM packages WHERE name = ?", (name,)
                ).fetchone()
                if row is None:
                    raise PackageNotFoundError(f"No package named '{name}' found")
                version = int(row["version"])
            row = self._conn.execute(
                "SELECT payload FROM package_versions WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        if row is None:
            raise PackageNotFoundError(f"Version {version} of package '{name}' not found")
        return ContextPackage.model_validate_json(row["payload"])

    def list_packages(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT name FROM packages ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def delete(self, name: str) -> None:
        if not is_valid_package_name(name):
            return
        name = name.strip()
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM package_versions WHERE name = ?", (name,))
            cur = self._conn.execute("DELETE FROM packages WHERE name = ?", (name,))
            self._conn.execute("COMMIT")
        if cur.rowcount:
            logger.info("Deleted package '%s' (sqlite)", name)
        else:
            logger.warning("Package '%s' not found for deletion", name)

    def exists(self, name: str) -> bool:
        if not is_valid_package_name(name):
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM packages WHERE name = ?", (name.strip(),)
            ).fetchone()
        return row is not None

    def list_versions(self, name: str) -> list[int]:
        if not is_valid_package_name(name):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT version FROM package_versions WHERE name = ? ORDER BY version",
                (name.strip(),),
            ).fetchall()
        return [int(r["version"]) for r in rows]

    def summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT name, version, schema_version, source_model, item_count,
                       created_at, updated_at
                FROM packages ORDER BY name
                """
            ).fetchall()
        return [
            {
                "name": r["name"],
                "version": int(r["version"]),
                "schema_version": int(r["schema_version"]),
                "source_model": r["source_model"] or "unknown",
                "total_items": int(r["item_count"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    # -- Maintenance ---------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            packages = self._conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
            versions = self._conn.execute("SELECT COUNT(*) FROM package_versions").fetchone()[0]
        size = self._db_path.stat().st_size if self._db_path.exists() else 0
        return {
            "backend": self.backend_name,
            "path": str(self._db_path),
            "packages": int(packages),
            "versions": int(versions),
            "bytes": int(size),
        }
