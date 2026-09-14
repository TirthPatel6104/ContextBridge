"""Tests for the Flask API (no network, temp SQLite store)."""

from __future__ import annotations

import io
import json

import pytest

from tests.conftest import SAMPLE_CHAT, SAMPLE_CHAT_WITH_SECRETS


def _upload(client, name="proj", text=SAMPLE_CHAT, filename="chat.txt", **fields):
    data = {"package_name": name, "model": "local", **fields}
    data["files[]"] = (io.BytesIO(text.encode("utf-8")), filename)
    return client.post("/api/extract", data=data, content_type="multipart/form-data")


class TestHealthAndPages:
    def test_index_renders(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert b"ContextBridge" in res.data

    def test_health(self, client):
        data = client.get("/api/health").get_json()
        assert data["ok"] and data["storage_backend"] == "sqlite"
        assert data["redact_by_default"] is True
        assert {k["kind"] for k in data["redaction_kinds"]} >= {"email", "api_key"}


class TestExtract:
    def test_extract_returns_items_with_provenance(self, client):
        res = _upload(client)
        assert res.status_code == 200, res.get_json()
        data = res.get_json()
        assert data["created"] and data["version"] == 1
        assert data["engine"] == "local"
        items = [i for cat in data["memory"].values() for i in cat]
        assert items and all(i["id"] and i["origin"] == "chat.txt" for i in items)
        assert data["tokens_transcript"] > 0
        assert data["redaction"]["total"] == 0

    def test_extract_redacts_and_reports(self, client):
        data = _upload(client, text=SAMPLE_CHAT_WITH_SECRETS).get_json()
        assert data["redaction"]["total"] >= 2
        blob = json.dumps(data["memory"])
        assert "sk-proj-" not in blob
        raw = _upload(client, name="raw", text=SAMPLE_CHAT_WITH_SECRETS, redact="false")
        assert "tirth@example.com" in json.dumps(raw.get_json()["memory"])

    @pytest.mark.parametrize("name", ["../etc", "has space", "", "x" * 80])
    def test_rejects_bad_package_names(self, client, name):
        res = _upload(client, name=name)
        assert res.status_code == 400
        assert "error" in res.get_json()

    def test_rejects_unsupported_extension(self, client):
        res = _upload(client, filename="malware.exe")
        assert res.status_code == 400
        assert "Unsupported file type" in res.get_json()["error"]

    def test_rejects_missing_files(self, client):
        res = client.post(
            "/api/extract",
            data={"package_name": "p", "model": "local"},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400

    def test_upload_limit(self, client):
        client.application.config["MAX_CONTENT_LENGTH"] = 100
        res = _upload(client, text="x" * 1000)
        assert res.status_code == 413
        assert "limit" in res.get_json()["error"].lower()


class TestPackages:
    def test_list_get_delete(self, client):
        _upload(client)
        listing = client.get("/api/packages").get_json()["packages"]
        assert listing[0]["name"] == "proj" and listing[0]["total_items"] > 0
        detail = client.get("/api/packages/proj").get_json()
        assert detail["version"] == 1 and detail["history"][0]["version"] == 1
        assert detail["counts"]["decisions"] >= 1
        assert client.delete("/api/packages/proj").status_code == 200
        assert client.get("/api/packages/proj").status_code == 404

    def test_remove_items_and_history(self, client):
        detail = _upload(client).get_json()
        first = next(i for cat in detail["memory"].values() for i in cat)
        res = client.post("/api/packages/proj/items/remove", json={"ids": [first["id"]]})
        assert res.status_code == 200
        assert res.get_json()["version"] == 2
        hist = client.get("/api/packages/proj/history").get_json()["history"]
        assert hist[-1]["removed"] == 1
        bad = client.post("/api/packages/proj/items/remove", json={"ids": "nope"})
        assert bad.status_code == 400

    def test_rollback(self, client):
        _upload(client)
        _upload(client, text="User: I decided to use Rust.")
        res = client.post("/api/packages/proj/rollback", json={"version": 1})
        assert res.status_code == 200 and res.get_json()["version"] == 1
        assert client.post("/api/packages/proj/rollback", json={"version": 9}).status_code == 400
        assert client.post("/api/packages/proj/rollback", json={}).status_code == 400


class TestRetrieveAndPrompt:
    def test_retrieve_explains_selection(self, client):
        _upload(client)
        res = client.post(
            "/api/retrieve",
            json={"package_name": "proj", "query": "Pydantic data models", "top_k": 2},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["summary"]["method"] == "lexical"
        assert data["selected"][0]["matched_terms"]
        assert data["selected"][0]["reason"]
        assert data["selected"][0]["item"]["category"]
        assert data["summary"]["tokens_saved_vs_full_memory"] >= 0

    def test_retrieve_options_validation(self, client):
        _upload(client)
        ok = client.post(
            "/api/retrieve",
            json={"package_name": "proj", "query": "x", "categories": "decisions,facts"},
        )
        assert ok.status_code == 200
        bad = client.post(
            "/api/retrieve", json={"package_name": "proj", "query": "x", "categories": ["nope"]}
        )
        assert bad.status_code == 400
        bad = client.post("/api/retrieve", json={"package_name": "proj", "top_k": 0})
        assert bad.status_code == 400

    def test_prompt_full_and_narrowed(self, client):
        _upload(client)
        full = client.post("/api/prompt", json={"package_name": "proj", "target_model": "claude"})
        assert full.status_code == 200
        body = full.get_json()
        assert "<context>" in body["prompt"] and body["retrieval"] is None
        assert body["target_name"] == "Claude"
        narrowed = client.post(
            "/api/prompt",
            json={
                "package_name": "proj",
                "target_model": "openai",
                "query": "FAISS vector search",
                "top_k": 1,
            },
        ).get_json()
        assert narrowed["retrieval"]["selected"] == 1
        assert narrowed["tokens_prompt"] < narrowed["tokens_full_prompt"]

    def test_prompt_without_items_is_400(self, client):
        _upload(client, name="blank", text="User: nothing")
        res = client.post("/api/prompt", json={"package_name": "blank", "target_model": "claude"})
        assert res.status_code == 400

    def test_prompt_missing_package(self, client):
        res = client.post("/api/prompt", json={"package_name": "nope", "target_model": "claude"})
        assert res.status_code == 404


class TestPortabilityMergeScanEval:
    def test_export_and_import(self, client):
        _upload(client)
        res = client.get("/api/packages/proj/export")
        assert res.status_code == 200
        assert "attachment" in res.headers["Content-Disposition"]
        exported = res.data
        again = client.post(
            "/api/packages/import",
            data={"file": (io.BytesIO(exported), "proj.contextbridge.json"), "rename": "copy"},
            content_type="multipart/form-data",
        )
        assert again.status_code == 200 and again.get_json()["package_name"] == "copy"
        dup = client.post("/api/packages/import", json=json.loads(exported))
        assert dup.status_code == 400  # exists without overwrite
        ok = client.post("/api/packages/import", json={**json.loads(exported), "overwrite": True})
        assert ok.status_code == 200

    def test_merge_dry_run(self, client):
        _upload(client, name="a")
        _upload(client, name="b", text=SAMPLE_CHAT + "\n\nUser: I decided to use Rust.")
        res = client.post(
            "/api/packages/merge", json={"names": ["a", "b"], "new_name": "ab", "dry_run": True}
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["dry_run"] and data["summary"]["duplicate_groups"] >= 1
        assert client.get("/api/packages/ab").status_code == 404
        res = client.post("/api/packages/merge", json={"names": ["a", "b"], "new_name": "ab"})
        assert res.status_code == 200
        assert client.get("/api/packages/ab").status_code == 200

    def test_scan(self, client):
        res = client.post("/api/redaction/scan", json={"text": "mail a@b.io"})
        assert res.status_code == 200
        assert res.get_json()["summary"]["counts"] == {"email": 1}
        assert client.post("/api/redaction/scan", json={}).status_code == 400

    def test_eval_endpoint(self, client):
        data = client.get("/api/eval").get_json()
        assert "aggregate" in data and data["fixtures"]


class TestSecurity:
    def test_cors_only_for_allowed_origins(self, client):
        allowed = client.get("/api/packages", headers={"Origin": "https://claude.ai"})
        assert allowed.headers.get("Access-Control-Allow-Origin") == "https://claude.ai"
        denied = client.get("/api/packages", headers={"Origin": "https://evil.example"})
        assert denied.headers.get("Access-Control-Allow-Origin") is None

    def test_internal_errors_do_not_leak_details(self, client, service, monkeypatch):
        def boom():
            raise RuntimeError("secret value sk-live-abcdefghijklmnopqrstuvwxyz")

        monkeypatch.setattr(service, "list_packages", boom)
        res = client.get("/api/packages")
        assert res.status_code == 500
        assert "sk-live" not in res.get_data(as_text=True)

    def test_attach_files_requires_valid_package(self, client):
        res = client.post(
            "/api/files/attach",
            data={"package_name": "nope", "files[]": (io.BytesIO(b"x"), "n.txt")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 404
        _upload(client)
        res = client.post(
            "/api/files/attach",
            data={"package_name": "proj", "files[]": (io.BytesIO(b"notes"), "../../n.txt")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 200
        assert res.get_json()["files_attached"][0]["stored_as"] == "n.txt"
        files = client.get("/api/files/proj").get_json()["files"]
        assert files[0]["name"] == "n.txt"
        # Files are only included in prompts when explicitly requested
        plain = client.post(
            "/api/prompt", json={"package_name": "proj", "target_model": "claude"}
        ).get_json()
        assert "notes" not in plain["prompt"]
        with_files = client.post(
            "/api/prompt",
            json={"package_name": "proj", "target_model": "claude", "include_files": True},
        ).get_json()
        assert "notes" in with_files["prompt"] and with_files["attached_files"] == ["n.txt"]
