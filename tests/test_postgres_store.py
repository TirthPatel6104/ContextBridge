"""Integration tests for the PostgreSQL + pgvector backend.

They run only when ``CB_TEST_DATABASE_URL`` points at a database with the
``vector`` extension available (CI starts ``pgvector/pgvector:pg16`` as a
service container; locally: ``docker compose up db``).  Every test uses its
own schema so runs can be repeated.
"""

from __future__ import annotations

import os
import uuid

import pytest

from contextbridge.config import Settings
from contextbridge.models import EgressRecord, RetrievalOptions
from contextbridge.service import ContextBridgeService
from contextbridge.storage import copy_store, get_store
from contextbridge.storage.base import PackageNotFoundError
from contextbridge.storage.sqlite_store import SQLiteStore
from contextbridge.validation import ValidationError
from tests.conftest import SAMPLE_CHAT, MockAdapter

DSN = os.getenv("CB_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="CB_TEST_DATABASE_URL not set")

psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def pg():
    """A PostgresStore bound to a throw-away schema."""
    from contextbridge.storage.postgres_store import PostgresStore

    schema = "t_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as conn:
        # The extension lives once per database; keep it in public so every
        # throw-away schema (search_path: <schema>, public) can see the type.
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in DSN else "?"
    dsn = f"{DSN}{sep}options=-c%20search_path%3D{schema}%2Cpublic"
    store = PostgresStore(dsn, max_size=3)
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pg_service(pg, tmp_path):
    return ContextBridgeService(pg, settings=Settings(storage_dir=tmp_path))


def test_migrations_apply_once(pg):
    assert pg.schema_version >= 2
    assert pg.migrate() == []
    stats = pg.stats()
    assert stats["pgvector"] and stats["packages"] == 0 and stats["schema_version"] >= 2
    assert pg.ping() is True
    assert "***" in pg.location() or "@" not in pg.location()


def test_save_load_versions_delete(pg_service, pg):
    outcome = pg_service.import_text(SAMPLE_CHAT, "proj", origin="chat.txt")
    assert outcome.created and pg.exists("proj")
    pg_service.import_text(SAMPLE_CHAT + "\n\nUser: I decided to use Rust.", "proj")
    assert pg.list_versions("proj") == [1, 2]
    assert pg.load("proj").version == 2 and pg.load("proj", version=1).version == 1
    assert pg.summaries()[0]["name"] == "proj" and pg.summaries()[0]["total_items"] > 0
    assert pg.list_packages() == ["proj"]
    with pytest.raises(PackageNotFoundError):
        pg.load("proj", version=9)
    with pytest.raises(PackageNotFoundError):
        pg.load("missing")
    assert pg.exists("../bad") is False and pg.list_versions("../bad") == []
    pg.delete("../bad")  # ignored
    pg.delete("proj")
    assert not pg.exists("proj") and pg.list_packages() == []
    pg.delete("proj")  # already gone: warning only


def test_egress_ledger(pg_service, pg):
    pg_service.import_text(SAMPLE_CHAT, "proj")
    built = pg_service.build_prompt("proj", "claude", surface="api")
    assert built["egress_id"] is not None
    pg_service.build_prompt("proj", "llama3", query="FAISS", surface="cli")
    records = pg.egress_records("proj")
    assert len(records) == 2 and records[0].surface == "cli" and records[0].target_kind == "local"
    assert records[1].item_ids and records[1].tokens > 0
    audit = pg_service.audit("proj")
    assert audit["summary"]["events"] == 2 and audit["summary"]["items_shared_with_cloud"] > 0
    assert pg.egress_records("../x") == [] and pg.clear_egress("../x") == 0
    assert pg.clear_egress("proj") == 2 and pg.egress_records("proj") == []
    with pytest.raises(ValidationError):
        pg.record_egress(
            EgressRecord(
                package_name="..", package_version=1, target_model="x", target_kind="cloud"
            )
        )


def test_pgvector_search_exact_and_indexed(pg):
    from contextbridge.models import ContextPackage

    pg.save(ContextPackage(name="vec"))
    dim = 8
    rows = [
        (f"item{i}", f"h{i}", [1.0 if j == i % dim else 0.0 for j in range(dim)]) for i in range(20)
    ]
    assert pg.upsert_embeddings("vec", rows, model="m") == 20
    assert pg.upsert_embeddings("vec", [], model="m") == 0
    assert pg.embedding_count("vec") == 20 and pg.embedding_count("vec", model="m") == 20
    assert pg.embedding_count("vec", model="other") == 0
    assert pg.embedding_hashes("vec", model="m")["item3"] == "h3"
    query = [0.0] * dim
    query[3] = 1.0
    exact = pg.search_embeddings("vec", query, top_k=3, model="m", exact=True)
    approx = pg.search_embeddings("vec", query, top_k=3, model="m")
    assert exact[0][1] == pytest.approx(1.0) and exact[0][0] in {"item3", "item11", "item19"}
    assert {i for i, _ in approx} == {i for i, _ in exact}
    assert pg.ensure_hnsw_index(dim) is False  # already built by upsert
    pg.drop_hnsw_index(dim)
    assert pg.ensure_hnsw_index(dim) is True
    assert pg.search_embeddings("vec", [], model="m") == []
    with pytest.raises(ValueError):
        pg.upsert_embeddings("vec", [("a", "h", [1.0]), ("b", "h", [1.0, 2.0])])
    assert pg.delete_embeddings("vec", model="m", item_ids=["item0", "item1"]) == 2
    assert pg.delete_embeddings("vec", model="m", item_ids=[]) == 0
    assert pg.delete_embeddings("vec") == 18
    # Bulk path (COPY) above the threshold, then an upsert that overwrites a hash.
    bulk = [(f"b{i}", "old", [float(i % dim == j) for j in range(dim)]) for i in range(150)]
    assert pg.upsert_embeddings("vec", bulk, model="bulk") == 150
    assert pg.upsert_embeddings("vec", [("b1", "new", bulk[1][2])], model="bulk") == 1
    hashes = pg.embedding_hashes("vec", model="bulk")
    assert len(hashes) == 150 and hashes["b1"] == "new" and hashes["b2"] == "old"
    assert pg.search_embeddings("vec", bulk[3][2], top_k=1, model="bulk")[0][1] == pytest.approx(
        1.0
    )
    assert pg.delete_embeddings("vec", model="bulk") == 150
    assert pg.embedding_hashes("../x") == {} and pg.embedding_count("../x") == 0
    assert pg.delete_embeddings("../x") == 0
    pg.delete("vec")


def test_retriever_reuses_persisted_embeddings(pg_service, pg):
    pg_service.import_text(SAMPLE_CHAT, "proj")
    adapter = MockAdapter()
    result = pg_service.retrieve(
        "proj", "Pydantic data models", RetrievalOptions(top_k=3), adapter=adapter
    )
    assert result.method == "hybrid" and result.selected
    first_calls = adapter.embed_calls
    total = pg_service.get_package("proj").memory.total_items
    assert pg.embedding_count("proj", model="mock") == total
    # Second query: only the query itself is embedded.
    pg_service.retrieve("proj", "vector search", RetrievalOptions(top_k=3), adapter=adapter)
    assert adapter.embed_calls == first_calls + 1
    # Removing an item drops its vector; re-import of a changed item re-embeds it.
    pkg = pg_service.get_package("proj")
    victim = pkg.memory.all_items[0].id
    pg_service.remove_items("proj", [victim])
    pg_service.retrieve("proj", "anything", adapter=adapter)
    assert pg.embedding_count("proj", model="mock") == total - 1
    assert victim not in pg.embedding_hashes("proj", model="mock")
    store = pg_service.vector_store_for("proj", adapter)
    assert store.persistent and store.size == total - 1
    store.clear()
    assert store.size == 0
    with pytest.raises(ValueError):
        store.add(["a"], [])
    store.add([], [])


def test_get_store_and_copy_from_sqlite(tmp_path, pg):
    sqlite = SQLiteStore(base_dir=tmp_path)
    svc = ContextBridgeService(sqlite, settings=Settings(storage_dir=tmp_path))
    svc.import_text(SAMPLE_CHAT, "one")
    svc.import_text(SAMPLE_CHAT + "\n\nUser: I decided to use Rust.", "one")
    svc.import_text(SAMPLE_CHAT, "two")
    svc.build_prompt("one", "claude")
    report = copy_store(sqlite, pg)
    assert report["copied"] == ["one", "two"] and report["failed"] == []
    assert pg.list_versions("one") == [1, 2] and pg.load("one").version == 2
    assert len(pg.egress_records("one")) == 1
    assert copy_store(sqlite, pg)["skipped"] == ["one", "two"]
    assert copy_store(sqlite, pg, overwrite=True, names=["two"])["copied"] == ["two"]
    sqlite.close()

    settings = Settings(storage_dir=tmp_path, storage_backend="postgres", database_url=pg._dsn)
    store = get_store(settings=settings)
    assert store.backend_name == "postgres" and store.exists("one")
    store.close()
    with pytest.raises(ValueError):
        get_store("postgres", settings=Settings(storage_dir=tmp_path))
    with pytest.raises(ValueError):
        get_store("nope", settings=Settings(storage_dir=tmp_path))


def test_api_readiness_against_postgres(pg_service, tmp_path):
    from fastapi.testclient import TestClient

    from contextbridge.api import create_app

    app = create_app(Settings(storage_dir=tmp_path), service=pg_service, configure_tracing=False)
    with TestClient(app) as client:
        assert client.get("/readyz").json()["storage_backend"] == "postgres"
        assert client.get("/api/v1/ready").json()["checks"]["storage"] is True
        pg_service.import_text(SAMPLE_CHAT, "proj")
        emb = client.get("/api/v1/packages/proj/embeddings").json()
        assert emb["persistent"] is True and emb["embeddings"] == 0
