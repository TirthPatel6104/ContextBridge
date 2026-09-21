"""CLI tests for the 0.4 operational commands: serve, migrate --to postgres, db, eval --golden."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from contextbridge.cli import main
from tests.conftest import SAMPLE_CHAT

DSN = os.getenv("CB_TEST_DATABASE_URL", "")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def storage(tmp_path):
    return str(tmp_path / "store")


def _run(runner, storage, *args, env=None):
    return runner.invoke(
        main, ["--storage-dir", storage, *args], env=env or {}, catch_exceptions=False
    )


def test_eval_golden_table_and_json(runner, storage, tmp_path):
    res = _run(runner, storage, "eval", "--golden")
    assert res.exit_code == 0, res.output
    assert "Golden retrieval harness" in res.output and "semantic" in res.output
    out = tmp_path / "g.json"
    res = _run(runner, storage, "eval", "--golden", "--json", "--top-k", "2", "-o", str(out))
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["top_k"] == 2 and data["pairs"] >= 50
    assert json.loads(out.read_text())["pairs"] == data["pairs"]


def test_eval_golden_baseline_gate(runner, storage, tmp_path):
    res = _run(runner, storage, "eval", "--golden", "--check-baseline")
    assert res.exit_code == 0 and "No regression" in res.output
    inflated = tmp_path / "base.json"
    res = _run(runner, storage, "eval", "--golden", "--update-baseline", str(inflated))
    assert res.exit_code == 0 and inflated.exists()
    data = json.loads(inflated.read_text())
    data["aggregate"]["mrr"] = 0.999
    inflated.write_text(json.dumps(data))
    res = _run(runner, storage, "eval", "--golden", "--check-baseline", "--baseline", str(inflated))
    assert res.exit_code == 1 and "regressed" in res.output
    res = _run(
        runner, storage, "eval", "--golden", "--check-baseline", "--baseline", str(tmp_path / "x")
    )
    assert res.exit_code == 1 and "No baseline" in res.output


def test_eval_offline_writes_output(runner, storage, tmp_path):
    out = tmp_path / "eval.json"
    res = _run(runner, storage, "eval", "-o", str(out))
    assert res.exit_code == 0 and "aggregate" in out.read_text()


def test_serve_help_and_missing_dsn(runner, storage):
    assert "FastAPI server" in _run(runner, storage, "serve", "--help").output
    res = _run(runner, storage, "migrate", "--to", "postgres", env={"CB_DATABASE_URL": ""})
    assert res.exit_code == 1 and "database-url" in res.output
    res = _run(runner, storage, "db", "status", env={"CB_DATABASE_URL": ""})
    assert res.exit_code == 1 and "database-url" in res.output
    res = _run(
        runner,
        storage,
        "migrate",
        "--to",
        "postgres",
        "--database-url",
        "postgresql://x:y@127.0.0.1:1/z",
    )
    assert res.exit_code == 1 and "Could not open PostgreSQL" in res.output
    res = _run(runner, storage, "db", "upgrade", "--database-url", "postgresql://x:y@127.0.0.1:1/z")
    assert res.exit_code == 1


def test_info_shows_sqlite_stats(runner, storage, tmp_path):
    chat = tmp_path / "chat.txt"
    chat.write_text(SAMPLE_CHAT, encoding="utf-8")
    assert _run(runner, storage, "import", str(chat), "-o", "proj").exit_code == 0
    res = _run(runner, storage, "info")
    assert res.exit_code == 0 and "Stored versions" in res.output


@pytest.mark.skipif(not DSN, reason="CB_TEST_DATABASE_URL not set")
def test_migrate_to_postgres_and_db_commands(runner, storage, tmp_path):
    import uuid

    import psycopg

    schema = "cli_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DSN, autocommit=True) as conn:
        # The extension lives once per database; keep it in public so every
        # throw-away schema (search_path: <schema>, public) can see the type.
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in DSN else "?"
    dsn = f"{DSN}{sep}options=-c%20search_path%3D{schema}%2Cpublic"
    try:
        chat = tmp_path / "chat.txt"
        chat.write_text(SAMPLE_CHAT, encoding="utf-8")
        assert _run(runner, storage, "import", str(chat), "-o", "proj").exit_code == 0
        res = _run(runner, storage, "db", "status", "--database-url", dsn)
        assert res.exit_code == 0 and "Pending migrations: 2" in res.output
        res = _run(runner, storage, "db", "upgrade", "--database-url", dsn)
        assert res.exit_code == 0 and "Applied 2" in res.output
        res = _run(runner, storage, "db", "upgrade", "--database-url", dsn)
        assert res.exit_code == 0 and "up to date" in res.output
        res = _run(runner, storage, "migrate", "--to", "postgres", "--database-url", dsn)
        assert res.exit_code == 0 and "Copied 1 package(s): proj" in res.output
        res = _run(runner, storage, "migrate", "--to", "postgres", "--database-url", dsn)
        assert res.exit_code == 0 and "Already in Postgres" in res.output
        res = _run(
            runner,
            storage,
            "migrate",
            "--to",
            "postgres",
            "--database-url",
            dsn,
            "--overwrite",
            "--only",
            "proj",
        )
        assert res.exit_code == 0 and "Copied 1" in res.output
        res = _run(runner, storage, "--backend", "postgres", "info", env={"CB_DATABASE_URL": dsn})
        assert res.exit_code == 0 and "postgres" in res.output and "embeddings stored" in res.output
        res = _run(
            runner,
            storage,
            "--backend",
            "postgres",
            "migrate",
            "--to",
            "postgres",
            env={"CB_DATABASE_URL": dsn},
        )
        assert res.exit_code == 1 and "already Postgres" in res.output
        res = _run(runner, storage, "db", "status", "--database-url", dsn)
        assert "Pending migrations: 0" in res.output
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
