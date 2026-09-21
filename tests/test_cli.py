"""Tests for the CLI (Click test runner, temp storage, offline engine)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from contextbridge import __version__
from contextbridge.cli import main
from tests.conftest import SAMPLE_CHAT, SAMPLE_CHAT_WITH_SECRETS


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CB_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("CB_STORAGE_BACKEND", raising=False)
    chat = tmp_path / "chat.txt"
    chat.write_text(SAMPLE_CHAT, encoding="utf-8")
    return tmp_path, chat


def _run(*args, input=None):
    return CliRunner().invoke(main, list(args), input=input, catch_exceptions=False)


class TestBasics:
    def test_version(self):
        result = _run("--version")
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_list_empty(self, env):
        result = _run("list")
        assert result.exit_code == 0
        assert "No context packages" in result.output

    def test_not_found_commands(self, env):
        for cmd in (["history", "nope"], ["inspect", "nope"], ["retrieve", "nope", "q"]):
            assert _run(*cmd).exit_code != 0

    def test_export_missing_file(self, env):
        result = CliRunner().invoke(
            main, ["export", "--model", "local", "--chat", "nonexistent.txt", "--output", "t"]
        )
        assert result.exit_code != 0


class TestWorkflow:
    def test_import_inspect_retrieve_prompt(self, env):
        tmp_path, chat = env
        result = _run("import", str(chat), "-o", "proj")
        assert result.exit_code == 0, result.output
        assert "Created package" in result.output

        listing = _run("list")
        assert "proj" in listing.output and "sqlite" in listing.output

        inspect = _run("inspect", "proj", "--ids")
        assert inspect.exit_code == 0 and "Decisions" in inspect.output

        retrieve = _run("retrieve", "proj", "Pydantic data models", "-k", "2")
        assert retrieve.exit_code == 0
        assert "Matched" in retrieve.output and "tokens selected" in retrieve.output

        as_json = _run("retrieve", "proj", "Pydantic", "--json")
        payload = json.loads(as_json.output)
        assert payload["selected"][0]["matched_terms"]

        prompt = _run("prompt", "proj", "--model", "claude", "--raw")
        assert prompt.exit_code == 0 and "<context>" in prompt.output

        narrowed = _run("prompt", "proj", "--model", "openai", "--raw", "-q", "FAISS", "-k", "1")
        assert "Selected by lexical retrieval" in narrowed.output

        history = _run("history", "proj")
        assert "v1" in history.output

    def test_import_reports_redaction_and_scan(self, env):
        tmp_path, _ = env
        secret_chat = tmp_path / "secret.txt"
        secret_chat.write_text(SAMPLE_CHAT_WITH_SECRETS, encoding="utf-8")
        scan = _run("scan", str(secret_chat))
        assert scan.exit_code == 0 and "api_key" in scan.output and "email" in scan.output
        assert "sk-proj-abc123def456" not in scan.output

        result = _run("import", str(secret_chat), "-o", "sec")
        assert "Redacted" in result.output
        raw = _run("prompt", "sec", "--model", "claude", "--raw")
        assert "[REDACTED:API_KEY]" in raw.output and "sk-proj-" not in raw.output

    def test_remove_rollback_delete(self, env):
        tmp_path, chat = env
        _run("import", str(chat), "-o", "proj")
        payload = json.loads(_run("retrieve", "proj", "", "--json").output)
        victim = payload["selected"][0]["item"]["id"]
        removed = _run("remove", "proj", victim)
        assert removed.exit_code == 0 and "Removed 1" in removed.output
        rolled = _run("rollback", "proj", "--version", "1")
        assert "v2 → v1" in rolled.output
        assert _run("rollback", "proj", "--version", "7").exit_code != 0
        deleted = _run("delete", "proj", "--yes")
        assert deleted.exit_code == 0 and "Deleted" in deleted.output

    def test_export_import_merge(self, env):
        tmp_path, chat = env
        _run("import", str(chat), "-o", "a")
        other = tmp_path / "other.txt"
        other.write_text(SAMPLE_CHAT + "\n\nUser: I decided to use Rust.", encoding="utf-8")
        _run("import", str(other), "-o", "b")

        out = tmp_path / "a.json"
        assert _run("export-package", "a", "-o", str(out)).exit_code == 0
        assert json.loads(out.read_text(encoding="utf-8"))["format"] == "contextbridge-package"
        imported = _run("import-package", str(out), "--name", "a_copy")
        assert imported.exit_code == 0 and "a_copy" in imported.output
        assert _run("import-package", str(out)).exit_code != 0  # exists

        dry = _run("merge", "a", "b", "-o", "ab", "--dry-run")
        assert dry.exit_code == 0 and "Dry run" in dry.output
        assert "ab" not in _run("list").output.split("a_copy")[0]
        real = _run("merge", "a", "b", "-o", "ab")
        assert "Saved merged package" in real.output

    def test_info_migrate_eval(self, env):
        tmp_path, chat = env
        info = _run("info")
        assert info.exit_code == 0 and "sqlite" in info.output

        # Create a legacy JSON package, then migrate it into SQLite.
        legacy = CliRunner().invoke(
            main, ["--backend", "json", "import", str(chat), "-o", "legacy"], catch_exceptions=False
        )
        assert legacy.exit_code == 0
        migrated = _run("migrate")
        assert migrated.exit_code == 0 and "legacy" in migrated.output
        assert "Already in SQLite: legacy" in _run("migrate").output
        assert "legacy" in _run("list").output

        evaluation = _run("eval")
        assert evaluation.exit_code == 0 and "Retrieval MRR" in evaluation.output
        report = json.loads(_run("eval", "--json").output)
        assert "aggregate" in report

    def test_web_export_reads_stdin(self, env):
        result = _run("web-export", "-o", "pasted", input=SAMPLE_CHAT)
        assert result.exit_code == 0 and "Created package" in result.output


class TestTrustCommands:
    """Sharing boundaries, lifecycle, ledger, and health from the terminal."""

    @staticmethod
    def _ids(output: str) -> list[str]:
        import re

        return list(dict.fromkeys(re.findall(r"\b[0-9a-f]{12}\b", output)))

    def test_share_withholds_and_audit_records(self, env):
        tmp_path, chat = env
        assert _run("import", str(chat), "-o", "proj").exit_code == 0
        ids = self._ids(_run("inspect", "proj", "--ids").output)
        assert ids

        shared = _run("share", "proj", ids[0], "--policy", "local_only")
        assert shared.exit_code == 0, shared.output
        assert "local_only" in shared.output
        again = _run("share", "proj", ids[0], "--policy", "local_only")
        assert "Nothing changed" in again.output
        assert _run("share", "proj", "zzz", "--policy", "never").exit_code == 2

        cloud = _run("prompt", "proj", "--model", "claude")
        assert cloud.exit_code == 0, cloud.output
        assert "withheld from this target" in cloud.output
        assert "egress ledger" in cloud.output
        local = _run("prompt", "proj", "--model", "ollama", "--dry-run")
        assert "withheld" not in local.output and "Dry run" in local.output

        audit = _run("audit", "proj")
        assert audit.exit_code == 0, audit.output
        assert "Events: 1" in audit.output and "claude" in audit.output
        as_json = json.loads(_run("audit", "proj", "--json").output)
        assert as_json["summary"]["events"] == 1
        assert as_json["events"][0]["surface"] == "cli"
        assert as_json["events"][0]["withheld_count"] == 1
        cleared = _run("audit", "proj", "--clear")
        assert "Cleared 1" in cleared.output
        assert "No prompts have been built" in _run("audit", "proj").output

    def test_done_reopen_supersede_and_health(self, env):
        tmp_path, chat = env
        _run("import", str(chat), "-o", "proj")
        ids = self._ids(_run("inspect", "proj", "--ids").output)
        assert _run("done", "proj", ids[0]).exit_code == 0
        inspected = _run("inspect", "proj")
        assert "done" in inspected.output
        assert _run("reopen", "proj", ids[0]).exit_code == 0
        sup = _run("supersede", "proj", ids[0], ids[1])
        assert sup.exit_code == 0 and "superseded by" in sup.output
        assert _run("supersede", "proj", ids[1], ids[0]).exit_code == 2

        health = _run("health", "proj", "--stale-days", "0")
        assert health.exit_code == 0, health.output
        assert "superseded 1" in health.output
        report = json.loads(_run("health", "proj", "--json").output)
        assert report["counts"]["status_superseded"] == 1
        history = _run("history", "proj")
        assert "Changed" in history.output

    def test_conflicts_flow(self, env):
        tmp_path, chat = env
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("User: I decided to use FAISS for vector search.", encoding="utf-8")
        b.write_text("User: I decided to switch to Qdrant for vector search.", encoding="utf-8")
        _run("import", str(a), "-o", "proj")
        second = _run("import", str(b), "-o", "proj")
        assert second.exit_code == 0, second.output
        assert "may contradict" in second.output

        listing = _run("conflicts", "proj")
        assert "stored:" in listing.output and "incoming:" in listing.output
        ids = self._ids(listing.output)
        resolved = _run("conflicts", "proj", "--resolve", ids[0], ids[1], "keep_new")
        assert resolved.exit_code == 0, resolved.output
        assert "Resolved (keep_new)" in resolved.output
        assert "No pending conflicts" in resolved.output
        assert _run("conflicts", "proj", "--resolve", ids[0], ids[1], "dismiss").exit_code == 2

    def test_handoff_import_skips_pasted_block(self, env):
        tmp_path, chat = env
        _run("import", str(chat), "-o", "proj")
        prompt = _run("prompt", "proj", "--model", "claude", "--raw", "--dry-run").output
        follow = tmp_path / "claude.txt"
        follow.write_text(
            "User: " + prompt + "\n\nAssistant: ok\n\nUser: I decided to use Rust.",
            encoding="utf-8",
        )
        result = _run("import", str(follow), "-o", "proj")
        assert result.exit_code == 0, result.output
        assert "Hand-off detected" in result.output
        assert "1 new" in result.output
