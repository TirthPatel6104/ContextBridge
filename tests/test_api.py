"""Tests for the FastAPI surface (no network, temp SQLite store)."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from contextbridge.api import create_app
from contextbridge.config import Settings
from tests.conftest import SAMPLE_CHAT, SAMPLE_CHAT_WITH_SECRETS


@pytest.fixture
def api(service, settings):
    app = create_app(settings, store=service.store, service=service, configure_tracing=False)
    with TestClient(app) as client:
        yield client


def _upload(client, name="proj", text=SAMPLE_CHAT, filename="chat.txt", prefix="/api", **fields):
    data = {"package_name": name, "model": "local", **fields}
    files = [("files[]", (filename, io.BytesIO(text.encode("utf-8")), "text/plain"))]
    return client.post(f"{prefix}/extract", data=data, files=files)


class TestProbesAndDocs:
    def test_index_and_static(self, api):
        res = api.get("/")
        assert res.status_code == 200 and "ContextBridge" in res.text
        assert api.get("/static/app.js").status_code == 200

    def test_livez_readyz(self, api):
        assert api.get("/livez").json()["ok"] is True
        ready = api.get("/readyz").json()
        assert ready["ready"] is True and ready["storage_backend"] == "sqlite"

    def test_openapi_documents_v1_only(self, api):
        spec = api.get("/openapi.json").json()
        paths = spec["paths"]
        assert "/api/v1/health" in paths and "/api/v1/packages/{name}/audit" in paths
        assert not any(p.startswith("/api/health") for p in paths)
        assert api.get("/docs").status_code == 200

    def test_health_both_prefixes(self, api):
        for prefix in ("/api", "/api/v1"):
            data = api.get(f"{prefix}/health").json()
            assert data["ok"] and data["storage_backend"] == "sqlite"
            assert data["api_key_required"] is False and data["tracing"] is False
            assert {k["kind"] for k in data["redaction_kinds"]} >= {"email", "api_key"}
        ready = api.get("/api/v1/ready").json()
        assert ready["ready"] and ready["checks"]["storage"]

    def test_security_headers(self, api):
        res = api.get("/api/v1/health")
        assert res.headers["X-Content-Type-Options"] == "nosniff"


class TestAuth:
    @pytest.fixture
    def secured(self, service, tmp_path):
        settings = Settings(storage_dir=tmp_path, api_key="s3cret")
        app = create_app(settings, store=service.store, service=service, configure_tracing=False)
        with TestClient(app) as client:
            yield client

    def test_requires_key(self, secured):
        res = secured.get("/api/v1/packages")
        assert res.status_code == 401 and "API key" in res.json()["error"]
        assert res.headers["WWW-Authenticate"] == "Bearer"
        assert secured.get("/livez").status_code == 200  # probes stay open

    def test_accepts_header_or_bearer(self, secured):
        assert secured.get("/api/v1/packages", headers={"X-API-Key": "s3cret"}).status_code == 200
        ok = secured.get("/api/packages", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        bad = secured.get("/api/packages", headers={"X-API-Key": "wrong"})
        assert bad.status_code == 401
        assert secured.get("/api/v1/health", headers={"X-API-Key": "s3cret"}).json()[
            "api_key_required"
        ]


class TestExtract:
    def test_extract_returns_items_with_provenance(self, api):
        res = _upload(api, prefix="/api/v1")
        assert res.status_code == 200, res.json()
        data = res.json()
        assert data["created"] and data["version"] == 1 and data["engine"] == "local"
        items = [i for cat in data["memory"].values() for i in cat]
        assert items and all(i["id"] and i["origin"] == "chat.txt" for i in items)
        assert data["tokens_transcript"] > 0 and data["redaction"]["total"] == 0

    def test_extract_redacts(self, api):
        data = _upload(api, text=SAMPLE_CHAT_WITH_SECRETS).json()
        assert data["redaction"]["total"] >= 2
        assert "sk-proj-" not in json.dumps(data["memory"])
        raw = _upload(api, name="raw", text=SAMPLE_CHAT_WITH_SECRETS, redact="false").json()
        assert "tirth@example.com" in json.dumps(raw["memory"])

    @pytest.mark.parametrize("name", ["../etc", "has space", "", "x" * 80])
    def test_rejects_bad_package_names(self, api, name):
        res = _upload(api, name=name)
        assert res.status_code == 400 and "error" in res.json()

    def test_rejects_unsupported_extension_and_missing_files(self, api):
        res = _upload(api, filename="malware.exe")
        assert res.status_code == 400 and "Unsupported file type" in res.json()["error"]
        res = api.post("/api/extract", data={"package_name": "p", "model": "local"})
        assert res.status_code == 400

    def test_upload_limit(self, service, tmp_path):
        settings = Settings(storage_dir=tmp_path, max_upload_bytes=100)
        app = create_app(settings, store=service.store, service=service, configure_tracing=False)
        with TestClient(app) as client:
            res = _upload(client, text="x" * 1000)
        assert res.status_code == 413 and "limit" in res.json()["error"].lower()


class TestPackages:
    def test_list_get_delete(self, api):
        _upload(api)
        listing = api.get("/api/v1/packages").json()["packages"]
        assert listing[0]["name"] == "proj" and listing[0]["total_items"] > 0
        detail = api.get("/api/v1/packages/proj").json()
        assert detail["version"] == 1 and detail["history"][0]["version"] == 1
        assert api.delete("/api/v1/packages/proj").status_code == 200
        assert api.get("/api/v1/packages/proj").status_code == 404

    def test_remove_items_history_rollback(self, api):
        detail = _upload(api).json()
        first = next(i for cat in detail["memory"].values() for i in cat)
        res = api.post("/api/packages/proj/items/remove", json={"ids": [first["id"]]})
        assert res.status_code == 200 and res.json()["version"] == 2
        hist = api.get("/api/packages/proj/history").json()["history"]
        assert hist[-1]["removed"] == 1
        bad = api.post("/api/packages/proj/items/remove", json={"ids": "nope"})
        assert bad.status_code == 400 and "error" in bad.json()
        res = api.post("/api/packages/proj/rollback", json={"version": 1})
        assert res.status_code == 200 and res.json()["version"] == 1
        assert api.post("/api/packages/proj/rollback", json={"version": 9}).status_code == 400
        assert api.post("/api/packages/proj/rollback", json={}).status_code == 400

    def test_sharing_status_supersede_conflicts_audit_health(self, api):
        detail = _upload(api).json()
        items = [i for cat in detail["memory"].values() for i in cat]
        a, b = items[0], items[1]
        res = api.post(
            "/api/v1/packages/proj/items/sharing", json={"ids": [a["id"]], "policy": "local_only"}
        )
        assert res.status_code == 200 and res.json()["changed"] == 1
        bad = api.post(
            "/api/v1/packages/proj/items/sharing", json={"ids": [a["id"]], "policy": "x"}
        )
        assert bad.status_code == 400
        res = api.post(
            "/api/v1/packages/proj/items/status", json={"ids": [b["id"]], "status": "done"}
        )
        assert res.status_code == 200 and res.json()["changed"] == 1
        task = next(i for i in items if i["category"] == "open_tasks")
        dec = next(i for i in items if i["category"] == "decisions")
        res = api.post(
            "/api/v1/packages/proj/items/supersede",
            json={"old_id": dec["id"], "new_id": task["id"]},
        )
        assert res.status_code == 200
        assert api.get("/api/v1/packages/proj/conflicts").json()["conflicts"] == []
        res = api.post(
            "/api/v1/packages/proj/conflicts/resolve",
            json={"existing_id": "a", "incoming_id": "b", "action": "dismiss"},
        )
        assert res.status_code == 400
        # A prompt build records egress; audit and health reflect it.
        built = api.post(
            "/api/v1/prompt", json={"package_name": "proj", "target_model": "claude"}
        ).json()
        assert built["withheld_count"] >= 2 and built["recorded"] is True
        audit = api.get("/api/v1/packages/proj/audit?limit=5").json()
        assert audit["summary"]["events"] == 1 and audit["events"][0]["surface"] == "api"
        health = api.get("/api/v1/packages/proj/health?stale_days=1").json()
        assert health["package_name"] == "proj"
        assert api.get("/api/v1/packages/proj/audit?limit=0").status_code == 400
        assert api.delete("/api/v1/packages/proj/audit").json()["removed"] == 1
        assert api.delete("/api/v1/packages/nope/audit").status_code == 404
        emb = api.get("/api/v1/packages/proj/embeddings").json()
        assert emb["persistent"] is False and emb["embeddings"] == 0


class TestRetrieveAndPrompt:
    def test_retrieve_explains_selection(self, api):
        _upload(api)
        res = api.post(
            "/api/v1/retrieve",
            json={"package_name": "proj", "query": "Pydantic data models", "top_k": 2},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["summary"]["method"] == "lexical"
        assert data["selected"][0]["matched_terms"] and data["selected"][0]["reason"]

    def test_retrieve_validation(self, api):
        _upload(api)
        ok = api.post(
            "/api/v1/retrieve",
            json={"package_name": "proj", "query": "x", "categories": "decisions,facts"},
        )
        assert ok.status_code == 200
        bad = api.post(
            "/api/v1/retrieve", json={"package_name": "proj", "query": "x", "categories": ["nope"]}
        )
        assert bad.status_code == 400
        bad = api.post("/api/v1/retrieve", json={"package_name": "proj", "top_k": 0})
        assert bad.status_code == 400 and "top_k" in bad.json()["error"]
        assert api.post("/api/v1/retrieve", json={}).status_code == 400

    def test_prompt_full_and_narrowed(self, api):
        _upload(api)
        full = api.post("/api/v1/prompt", json={"package_name": "proj", "target_model": "claude"})
        assert full.status_code == 200
        body = full.json()
        assert "<context>" in body["prompt"] and body["retrieval"] is None
        narrowed = api.post(
            "/api/v1/prompt",
            json={
                "package_name": "proj",
                "target_model": "openai",
                "query": "FAISS vector search",
                "top_k": 1,
                "record": False,
            },
        ).json()
        assert narrowed["retrieval"]["selected"] == 1 and narrowed["recorded"] is False
        assert narrowed["tokens_prompt"] < narrowed["tokens_full_prompt"]

    def test_prompt_errors(self, api):
        _upload(api, name="blank", text="User: nothing")
        res = api.post("/api/v1/prompt", json={"package_name": "blank", "target_model": "claude"})
        assert res.status_code == 400
        res = api.post("/api/v1/prompt", json={"package_name": "nope", "target_model": "claude"})
        assert res.status_code == 404


class TestPortabilityMergeScanEval:
    def test_export_and_import(self, api):
        _upload(api)
        res = api.get("/api/v1/packages/proj/export")
        assert res.status_code == 200 and "attachment" in res.headers["Content-Disposition"]
        exported = res.content
        again = api.post(
            "/api/v1/packages/import",
            data={"rename": "copy"},
            files={"file": ("proj.contextbridge.json", io.BytesIO(exported), "application/json")},
        )
        assert again.status_code == 200 and again.json()["package_name"] == "copy"
        dup = api.post("/api/v1/packages/import", json=json.loads(exported))
        assert dup.status_code == 400
        ok = api.post("/api/v1/packages/import", json={**json.loads(exported), "overwrite": True})
        assert ok.status_code == 200
        assert api.post("/api/v1/packages/import", content=b"nope").status_code == 400
        assert api.post("/api/v1/packages/import", data={"x": "1"}, files={}).status_code == 400

    def test_merge(self, api):
        _upload(api, name="a")
        _upload(api, name="b", text=SAMPLE_CHAT + "\n\nUser: I decided to use Rust.")
        res = api.post(
            "/api/v1/packages/merge", json={"names": ["a", "b"], "new_name": "ab", "dry_run": True}
        )
        assert res.status_code == 200 and res.json()["summary"]["duplicate_groups"] >= 1
        assert api.get("/api/v1/packages/ab").status_code == 404
        res = api.post("/api/v1/packages/merge", json={"names": ["a", "b"], "new_name": "ab"})
        assert res.status_code == 200 and api.get("/api/v1/packages/ab").status_code == 200
        assert (
            api.post("/api/v1/packages/merge", json={"names": ["a"], "new_name": "x"}).status_code
            == 400
        )

    def test_scan_eval_golden_models(self, api):
        res = api.post("/api/v1/redaction/scan", json={"text": "mail a@b.io"})
        assert res.status_code == 200 and res.json()["summary"]["counts"] == {"email": 1}
        assert api.post("/api/v1/redaction/scan", json={"text": "  "}).status_code == 400
        data = api.get("/api/v1/eval").json()
        assert "aggregate" in data and data["fixtures"]
        golden = api.get("/api/v1/eval/golden?top_k=1").json()
        assert golden["pairs"] >= 50 and golden["top_k"] == 1
        models = api.get("/api/v1/local/models").json()
        assert "models" in models


class TestSecurity:
    def test_cors_only_for_allowed_origins(self, api):
        allowed = api.get("/api/v1/packages", headers={"Origin": "https://claude.ai"})
        assert allowed.headers.get("access-control-allow-origin") == "https://claude.ai"
        denied = api.get("/api/v1/packages", headers={"Origin": "https://evil.example"})
        assert denied.headers.get("access-control-allow-origin") is None

    def test_internal_errors_do_not_leak(self, api, service, monkeypatch):
        def boom():
            raise RuntimeError("secret value sk-live-abcdefghijklmnopqrstuvwxyz")

        monkeypatch.setattr(service, "list_packages", boom)
        client = TestClient(api.app, raise_server_exceptions=False)
        res = client.get("/api/v1/packages")
        assert res.status_code == 500 and "sk-live" not in res.text

    def test_attach_files(self, api):
        res = api.post(
            "/api/v1/files/attach",
            data={"package_name": "nope"},
            files=[("files[]", ("n.txt", io.BytesIO(b"x"), "text/plain"))],
        )
        assert res.status_code == 404
        _upload(api)
        res = api.post(
            "/api/v1/files/attach",
            data={"package_name": "proj"},
            files=[("files[]", ("../../n.txt", io.BytesIO(b"notes"), "text/plain"))],
        )
        assert res.status_code == 200 and res.json()["files_attached"][0]["stored_as"] == "n.txt"
        assert api.post("/api/v1/files/attach", data={"package_name": "proj"}).status_code == 400
        assert api.get("/api/v1/files/proj").json()["files"][0]["name"] == "n.txt"
        assert api.get("/api/v1/watch/files").json()["files"] == []
        plain = api.post(
            "/api/v1/prompt", json={"package_name": "proj", "target_model": "claude"}
        ).json()
        assert "notes" not in plain["prompt"]
        with_files = api.post(
            "/api/v1/prompt",
            json={
                "package_name": "proj",
                "target_model": "claude",
                "include_files": True,
                "include_watch": True,
            },
        ).json()
        assert "notes" in with_files["prompt"] and with_files["attached_files"] == ["n.txt"]
