"""Versioned SQL migrations for the PostgreSQL backend.

Migrations are plain ``.sql`` files shipped inside the package
(``contextbridge/storage/sql/NNNN_name.sql``).  Each file is applied
once, inside its own transaction, and recorded in ``schema_migrations`` so
that opening the same database twice is a no-op.  There is deliberately no
"down" direction: every migration here is additive, and the payload format
is versioned separately by :data:`contextbridge.models.SCHEMA_VERSION`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Any

logger = logging.getLogger(__name__)

MIGRATIONS_PACKAGE = "contextbridge.storage.sql"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def load_migrations() -> list[Migration]:
    """All bundled migrations, sorted by version."""
    found: list[Migration] = []
    root = resources.files(MIGRATIONS_PACKAGE)
    for entry in root.iterdir():
        match = _FILENAME.match(entry.name)
        if not match:
            continue
        sql = entry.read_text(encoding="utf-8")
        found.append(Migration(version=int(match.group(1)), name=match.group(2), sql=sql))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"Duplicate migration versions: {versions}")
    return found


def applied_versions(conn: Any) -> set[int]:
    """Versions already recorded in ``schema_migrations`` (creating the table if needed)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute("SELECT version FROM schema_migrations")
        rows = cur.fetchall()
    conn.commit()
    return {int(r[0]) for r in rows}


def pending_migrations(conn: Any) -> list[Migration]:
    done = applied_versions(conn)
    return [m for m in load_migrations() if m.version not in done]


def apply_migrations(conn: Any, *, upto: int | None = None) -> list[Migration]:
    """Apply every pending migration (optionally only up to *upto*); returns what ran."""
    ran: list[Migration] = []
    for migration in pending_migrations(conn):
        if upto is not None and migration.version > upto:
            break
        logger.info("Applying migration %s", migration.label)
        try:
            with conn.cursor() as cur:
                cur.execute(migration.sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, datetime.now(UTC)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ran.append(migration)
    return ran


def current_version(conn: Any) -> int:
    done = applied_versions(conn)
    return max(done) if done else 0


__all__ = [
    "Migration",
    "load_migrations",
    "applied_versions",
    "pending_migrations",
    "apply_migrations",
    "current_version",
]
