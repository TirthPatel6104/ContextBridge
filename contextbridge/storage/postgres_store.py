"""PostgreSQL + pgvector storage backend.

The relational layout mirrors :mod:`contextbridge.storage.sqlite_store`
(``packages`` summary rows, every version as a JSONB payload, and the egress
ledger) so that the portable format, rollback, and export behave identically
on both backends.  On top of that, ``item_embeddings`` keeps one vector per
(package, embedding model, item) so semantic retrieval does not have to
re-embed a package on every request, and pgvector's HNSW index answers
nearest-neighbour queries for packages far larger than fit in a per-request
FAISS index.

Schema changes are applied by :mod:`contextbridge.storage.migrations` when
the store is opened (``migrate=True``).

Connections come from a small :class:`psycopg_pool.ConnectionPool`, which is
what makes the store safe under a multi-worker ASGI server.  Every public
method borrows a connection for the duration of one transaction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from contextbridge.models import ContextPackage, EgressRecord
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.storage.migrations import (
    apply_migrations,
    current_version,
    load_migrations,
)
from contextbridge.telemetry import span
from contextbridge.validation import is_valid_package_name, validate_package_name

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "default"

#: HNSW build parameters.  ``m`` and ``ef_construction`` follow the pgvector
#: defaults; ``ef_search`` is raised at query time for better recall on
#: filtered scans (one package out of many).
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64
HNSW_EF_SEARCH = 100

#: Above this many rows, ``upsert_embeddings`` streams with COPY instead of
#: a parameterised INSERT per row.
COPY_THRESHOLD = 64


def _redact_dsn(dsn: str) -> str:
    """Hide the password in a DSN for logs and ``location()``."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(dsn)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
            return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:  # pragma: no cover - defensive
        return "postgres://***"


def content_hash(content: str) -> str:
    return hashlib.sha1(" ".join(content.split()).strip().lower().encode()).hexdigest()[:16]


def _vector_literal(values: Iterable[float]) -> str:
    """pgvector's text input format: ``[0.1,0.2,...]``."""
    return "[" + ",".join(f"{float(v):.8g}" for v in values) + "]"


class PostgresStore(StorageBackend):
    """PostgreSQL-backed :class:`StorageBackend` with pgvector embeddings."""

    backend_name = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        migrate: bool = True,
        connect_timeout: float = 10.0,
    ) -> None:
        if not dsn:
            raise ValueError("A PostgreSQL DSN is required (CB_DATABASE_URL)")
        try:
            import psycopg  # noqa: F401
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise RuntimeError(
                "The postgres backend needs 'psycopg[binary,pool]' and 'pgvector': "
                "pip install 'contextbridge[postgres]'"
            ) from exc
        self._dsn = dsn
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            timeout=connect_timeout,
            kwargs={"autocommit": False},
        )
        self._schema_version = 0
        self._hnsw_dimensions: set[int] = set()
        if migrate:
            self.migrate()
        else:
            with self._connection() as conn:
                self._schema_version = current_version(conn)

    # -- Connections ---------------------------------------------------------

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        with self._pool.connection() as conn:
            yield conn

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[Any]:
        """Borrow a connection and run one transaction (rollback on error)."""
        with self._pool.connection() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def migrate(self) -> list[str]:
        """Apply pending schema migrations; returns their labels."""
        with self._connection() as conn:
            ran = apply_migrations(conn)
            self._schema_version = current_version(conn)
        return [m.label for m in ran]

    @property
    def schema_version(self) -> int:
        return self._schema_version

    def schema_status(self) -> dict[str, Any]:
        """Applied vs. latest migration and pgvector availability (safe on an empty database)."""
        with self._connection() as conn:
            applied = current_version(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                installed = cur.fetchone()
                cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
                available = cur.fetchone() is not None
            conn.rollback()
        latest = max((m.version for m in load_migrations()), default=0)
        self._schema_version = applied
        return {
            "location": self.location(),
            "schema_version": applied,
            "latest": latest,
            "pending": max(0, latest - applied),
            "pgvector_installed": installed[0] if installed else None,
            "pgvector_available": available,
        }

    def location(self) -> str:
        return _redact_dsn(self._dsn)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._pool.close()

    def ping(self) -> bool:
        """``True`` when a trivial query succeeds (used by readiness probes)."""
        try:
            with self._connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as exc:
            logger.warning("Postgres ping failed (%s)", exc.__class__.__name__)
            return False

    # -- StorageBackend interface -------------------------------------------

    def save(self, package: ContextPackage) -> None:
        name = validate_package_name(package.name)
        payload = package.model_dump_json()
        with span("store.save", backend="postgres", package_name=name, version=package.version):
            with self._transaction() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO packages
                        (name, version, schema_version, source_model, item_count,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        version = EXCLUDED.version,
                        schema_version = EXCLUDED.schema_version,
                        source_model = EXCLUDED.source_model,
                        item_count = EXCLUDED.item_count,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        name,
                        package.version,
                        package.schema_version,
                        package.source_model,
                        package.memory.total_items,
                        package.created_at,
                        package.updated_at,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO package_versions (name, version, payload, saved_at)
                    VALUES (%s, %s, %s::jsonb, %s)
                    ON CONFLICT (name, version) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        saved_at = EXCLUDED.saved_at
                    """,
                    (name, package.version, payload, datetime.now(UTC)),
                )
        logger.info("Saved package '%s' v%d (postgres)", name, package.version)

    def load(self, name: str, *, version: int | None = None) -> ContextPackage:
        name = validate_package_name(name)
        with span("store.load", backend="postgres", package_name=name):
            with self._connection() as conn, conn.cursor() as cur:
                if version is None:
                    cur.execute("SELECT version FROM packages WHERE name = %s", (name,))
                    row = cur.fetchone()
                    if row is None:
                        raise PackageNotFoundError(f"No package named '{name}' found")
                    version = int(row[0])
                cur.execute(
                    "SELECT payload FROM package_versions WHERE name = %s AND version = %s",
                    (name, version),
                )
                row = cur.fetchone()
            conn.rollback()
        if row is None:
            raise PackageNotFoundError(f"Version {version} of package '{name}' not found")
        payload = row[0]
        if isinstance(payload, str | bytes):
            return ContextPackage.model_validate_json(payload)
        return ContextPackage.model_validate(payload)

    def list_packages(self) -> list[str]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT name FROM packages ORDER BY name")
            rows = cur.fetchall()
            conn.rollback()
        return [r[0] for r in rows]

    def delete(self, name: str) -> None:
        if not is_valid_package_name(name):
            return
        name = name.strip()
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM egress_log WHERE package_name = %s", (name,))
            cur.execute("DELETE FROM packages WHERE name = %s", (name,))  # cascades
            deleted = cur.rowcount
        if deleted:
            logger.info("Deleted package '%s' (postgres)", name)
        else:
            logger.warning("Package '%s' not found for deletion", name)

    def exists(self, name: str) -> bool:
        if not is_valid_package_name(name):
            return False
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM packages WHERE name = %s", (name.strip(),))
            row = cur.fetchone()
            conn.rollback()
        return row is not None

    def list_versions(self, name: str) -> list[int]:
        if not is_valid_package_name(name):
            return []
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM package_versions WHERE name = %s ORDER BY version",
                (name.strip(),),
            )
            rows = cur.fetchall()
            conn.rollback()
        return [int(r[0]) for r in rows]

    def summaries(self) -> list[dict[str, Any]]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, version, schema_version, source_model, item_count,
                       created_at, updated_at
                FROM packages ORDER BY name
                """
            )
            rows = cur.fetchall()
            conn.rollback()
        return [
            {
                "name": r[0],
                "version": int(r[1]),
                "schema_version": int(r[2]),
                "source_model": r[3] or "unknown",
                "total_items": int(r[4]),
                "created_at": r[5].isoformat(),
                "updated_at": r[6].isoformat(),
            }
            for r in rows
        ]

    # -- Egress ledger -------------------------------------------------------

    def record_egress(self, record: EgressRecord) -> EgressRecord:
        name = validate_package_name(record.package_name)
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO egress_log
                    (package_name, package_version, ts, target_model, target_kind,
                     surface, query, item_ids, item_count, withheld_count, tokens)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING id
                """,
                (
                    name,
                    record.package_version,
                    record.timestamp,
                    record.target_model,
                    record.target_kind,
                    record.surface,
                    record.query,
                    json.dumps(record.item_ids),
                    record.item_count,
                    record.withheld_count,
                    record.tokens,
                ),
            )
            record.id = int(cur.fetchone()[0])
        return record

    def egress_records(self, name: str, *, limit: int = 200) -> list[EgressRecord]:
        if not is_valid_package_name(name):
            return []
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts, package_name, package_version, target_model, target_kind,
                       surface, query, item_ids, item_count, withheld_count, tokens
                FROM egress_log WHERE package_name = %s
                ORDER BY ts DESC, id DESC LIMIT %s
                """,
                (name.strip(), int(limit)),
            )
            rows = cur.fetchall()
            conn.rollback()
        return [
            EgressRecord(
                id=int(r[0]),
                timestamp=r[1],
                package_name=r[2],
                package_version=int(r[3]),
                target_model=r[4],
                target_kind=r[5],
                surface=r[6],
                query=r[7],
                item_ids=list(r[8] or []),
                item_count=int(r[9]),
                withheld_count=int(r[10]),
                tokens=int(r[11]),
            )
            for r in rows
        ]

    def clear_egress(self, name: str) -> int:
        if not is_valid_package_name(name):
            return 0
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM egress_log WHERE package_name = %s", (name.strip(),))
            return int(cur.rowcount)

    # -- Embeddings (pgvector) ----------------------------------------------

    def ensure_hnsw_index(self, dimension: int) -> bool:
        """Create the HNSW index for vectors of *dimension* (idempotent).

        pgvector can only index columns with a fixed dimension, so the index
        is an expression index over ``embedding::vector(N)`` restricted to
        rows with that dimension.  Queries use the same expression so the
        planner can pick it up.
        """
        if dimension in self._hnsw_dimensions:
            return False
        name = f"idx_item_embeddings_hnsw_{int(dimension)}"
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {name} ON item_embeddings
                USING hnsw ((embedding::vector({int(dimension)})) vector_cosine_ops)
                WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})
                WHERE dimension = {int(dimension)}
                """
            )
        self._hnsw_dimensions.add(dimension)
        return True

    def drop_hnsw_index(self, dimension: int) -> None:
        name = f"idx_item_embeddings_hnsw_{int(dimension)}"
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {name}")
        self._hnsw_dimensions.discard(dimension)

    def upsert_embeddings(
        self,
        package_name: str,
        rows: Iterable[tuple[str, str, list[float]]],
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        index: bool = True,
    ) -> int:
        """Store ``(item_id, content_hash, vector)`` rows; returns how many were written."""
        name = validate_package_name(package_name)
        rows = list(rows)
        if not rows:
            return 0
        dims = {len(vec) for _, _, vec in rows}
        if len(dims) != 1 or 0 in dims:
            raise ValueError("All embeddings must be non-empty and share one dimension")
        dimension = dims.pop()
        now = datetime.now(UTC)
        with span(
            "store.upsert_embeddings",
            backend="postgres",
            package_name=name,
            count=len(rows),
            dimension=dimension,
        ):
            with self._transaction() as conn, conn.cursor() as cur:
                if len(rows) <= COPY_THRESHOLD:
                    cur.executemany(
                        """
                        INSERT INTO item_embeddings
                            (package_name, item_id, model, dimension, embedding, content_hash,
                             updated_at)
                        VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
                        ON CONFLICT (package_name, model, item_id) DO UPDATE SET
                            dimension = EXCLUDED.dimension,
                            embedding = EXCLUDED.embedding,
                            content_hash = EXCLUDED.content_hash,
                            updated_at = EXCLUDED.updated_at
                        """,
                        [
                            (name, item_id, model, dimension, _vector_literal(vec), chash, now)
                            for item_id, chash, vec in rows
                        ],
                    )
                else:
                    # Bulk path: stream rows with COPY into a temp table, then merge.
                    cur.execute(
                        """
                        CREATE TEMP TABLE item_embeddings_stage (
                            item_id TEXT, content_hash TEXT, embedding TEXT
                        ) ON COMMIT DROP
                        """
                    )
                    with cur.copy(
                        "COPY item_embeddings_stage (item_id, content_hash, embedding) FROM STDIN"
                    ) as copy:
                        for item_id, chash, vec in rows:
                            copy.write_row((item_id, chash, _vector_literal(vec)))
                    cur.execute(
                        """
                        INSERT INTO item_embeddings
                            (package_name, item_id, model, dimension, embedding, content_hash,
                             updated_at)
                        SELECT %s, item_id, %s, %s, embedding::vector, content_hash, %s
                        FROM item_embeddings_stage
                        ON CONFLICT (package_name, model, item_id) DO UPDATE SET
                            dimension = EXCLUDED.dimension,
                            embedding = EXCLUDED.embedding,
                            content_hash = EXCLUDED.content_hash,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (name, model, dimension, now),
                    )
        if index:
            self.ensure_hnsw_index(dimension)
        return len(rows)

    def embedding_hashes(
        self, package_name: str, *, model: str = DEFAULT_EMBEDDING_MODEL
    ) -> dict[str, str]:
        """``{item_id: content_hash}`` for the vectors already stored."""
        if not is_valid_package_name(package_name):
            return {}
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT item_id, content_hash FROM item_embeddings "
                "WHERE package_name = %s AND model = %s",
                (package_name.strip(), model),
            )
            rows = cur.fetchall()
            conn.rollback()
        return {r[0]: r[1] for r in rows}

    def embedding_count(self, package_name: str, *, model: str | None = None) -> int:
        if not is_valid_package_name(package_name):
            return 0
        with self._connection() as conn, conn.cursor() as cur:
            if model is None:
                cur.execute(
                    "SELECT COUNT(*) FROM item_embeddings WHERE package_name = %s",
                    (package_name.strip(),),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM item_embeddings WHERE package_name = %s AND model = %s",
                    (package_name.strip(), model),
                )
            count = cur.fetchone()[0]
            conn.rollback()
        return int(count)

    def delete_embeddings(
        self,
        package_name: str,
        *,
        model: str | None = None,
        item_ids: Iterable[str] | None = None,
    ) -> int:
        if not is_valid_package_name(package_name):
            return 0
        clauses = ["package_name = %s"]
        params: list[Any] = [package_name.strip()]
        if model is not None:
            clauses.append("model = %s")
            params.append(model)
        if item_ids is not None:
            ids = list(item_ids)
            if not ids:
                return 0
            clauses.append("item_id = ANY(%s)")
            params.append(ids)
        with self._transaction() as conn, conn.cursor() as cur:
            cur.execute(f"DELETE FROM item_embeddings WHERE {' AND '.join(clauses)}", params)
            return int(cur.rowcount)

    def search_embeddings(
        self,
        package_name: str,
        query: list[float],
        *,
        top_k: int = 10,
        model: str = DEFAULT_EMBEDDING_MODEL,
        exact: bool = False,
    ) -> list[tuple[str, float]]:
        """Nearest items by cosine similarity: ``[(item_id, similarity), ...]``.

        ``exact=True`` disables index scans for the query (used by the
        benchmark to measure HNSW recall against brute force).
        """
        name = validate_package_name(package_name)
        if not query:
            return []
        dimension = len(query)
        literal = _vector_literal(query)
        with span(
            "store.search_embeddings",
            backend="postgres",
            package_name=name,
            top_k=top_k,
            exact=exact,
            dimension=dimension,
        ):
            with self._connection() as conn, conn.cursor() as cur:
                if exact:
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute("SET LOCAL enable_bitmapscan = off")
                else:
                    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")
                    with contextlib.suppress(Exception):
                        # pgvector >= 0.8: keep scanning until the filter is satisfied.
                        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                # The query vector appears twice on purpose: the planner only
                # matches the HNSW expression index when ORDER BY compares the
                # indexed expression with a constant (a CTE / subquery hides it
                # and forces an exact scan).
                cur.execute(
                    f"""
                    SELECT item_id,
                           1 - (embedding::vector({dimension}) <=> %s::vector({dimension}))
                               AS similarity
                    FROM item_embeddings
                    WHERE package_name = %s AND model = %s AND dimension = %s
                    ORDER BY embedding::vector({dimension}) <=> %s::vector({dimension})
                    LIMIT %s
                    """,
                    (literal, name, model, dimension, literal, int(top_k)),
                )
                rows = cur.fetchall()
                conn.rollback()
        return [(r[0], float(r[1])) for r in rows]

    # -- Maintenance ---------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM packages")
            packages = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM package_versions")
            versions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM egress_log")
            egress = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM item_embeddings")
            embeddings = cur.fetchone()[0]
            cur.execute("SELECT pg_database_size(current_database())")
            size = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            conn.rollback()
        return {
            "backend": self.backend_name,
            "location": self.location(),
            "schema_version": self._schema_version,
            "pgvector": row[0] if row else None,
            "packages": int(packages),
            "versions": int(versions),
            "egress_events": int(egress),
            "embeddings": int(embeddings),
            "bytes": int(size),
        }


class PgVectorStore:
    """Adapter that lets :class:`~contextbridge.core.retriever.MemoryRetriever`
    use pgvector through the same ``add`` / ``search`` / ``clear`` surface as
    the in-memory :class:`~contextbridge.storage.vector_store.VectorStore`.

    Vectors persist across requests, so ``known_hashes()`` lets the retriever
    embed only items whose content changed since they were last indexed.
    """

    persistent = True

    def __init__(
        self, store: PostgresStore, package_name: str, *, model: str = DEFAULT_EMBEDDING_MODEL
    ) -> None:
        self._store = store
        self._package = validate_package_name(package_name)
        self._model = model
        self._dimension: int | None = None

    @property
    def dimension(self) -> int | None:
        return self._dimension

    @property
    def size(self) -> int:
        return self._store.embedding_count(self._package, model=self._model)

    def known_hashes(self) -> dict[str, str]:
        return self._store.embedding_hashes(self._package, model=self._model)

    def add(
        self, texts: list[str], embeddings: list[list[float]], hashes: list[str] | None = None
    ) -> None:
        """``texts`` are item ids here (the retriever indexes by id)."""
        if len(texts) != len(embeddings):
            raise ValueError("texts and embeddings must have the same length")
        if not texts:
            return
        hashes = hashes or [""] * len(texts)
        self._dimension = len(embeddings[0])
        self._store.upsert_embeddings(
            self._package, zip(texts, hashes, embeddings), model=self._model
        )

    def search(self, query_embedding: list[float], *, top_k: int = 5) -> list[tuple[str, float]]:
        return self._store.search_embeddings(
            self._package, list(query_embedding), top_k=top_k, model=self._model
        )

    def clear(self) -> None:
        self._store.delete_embeddings(self._package, model=self._model)

    def remove(self, item_ids: Iterable[str]) -> int:
        return self._store.delete_embeddings(self._package, model=self._model, item_ids=item_ids)


__all__ = ["PostgresStore", "PgVectorStore", "content_hash", "DEFAULT_EMBEDDING_MODEL"]
