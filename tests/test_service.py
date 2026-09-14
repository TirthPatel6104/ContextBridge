"""Tests for the service layer (real workflows against a temp SQLite store)."""

from __future__ import annotations

import json

import pytest

from contextbridge.core.local_extractor import LocalExtractor
from contextbridge.models import MemoryCategory, RetrievalOptions
from contextbridge.service import ContextBridgeService
from contextbridge.storage.base import PackageNotFoundError
from contextbridge.validation import ValidationError
from tests.conftest import SAMPLE_CHAT, SAMPLE_CHAT_WITH_SECRETS, MockAdapter


class TestImport:
    def test_import_creates_package_with_provenance(self, service):
        outcome = service.import_text(SAMPLE_CHAT, "proj", origin="chat.txt")
        assert outcome.created and outcome.package.version == 1
        assert outcome.engine == "local"
        assert outcome.extracted.total_items > 0
        assert all(i.origin == "chat.txt" for i in outcome.extracted.all_items)
        assert outcome.transcript_tokens > 0
        assert service.store.exists("proj")

    def test_import_redacts_secrets_by_default(self, service):
        outcome = service.import_text(SAMPLE_CHAT_WITH_SECRETS, "secure")
        stored = service.get_package("secure")
        blob = json.dumps(service.memory_to_dict(stored.memory))
        assert "sk-proj-" not in blob and "tirth@example.com" not in blob
        assert outcome.redaction.total >= 2
        assert {"api_key", "email"} <= set(outcome.redaction.counts)
        assert any(i.was_redacted for i in stored.memory.all_items)

    def test_import_can_opt_out_of_redaction(self, service):
        outcome = service.import_text(SAMPLE_CHAT_WITH_SECRETS, "raw", redact=False)
        assert outcome.redaction.total == 0
        blob = json.dumps(service.memory_to_dict(outcome.package.memory))
        assert "tirth@example.com" in blob

    def test_append_and_replace_modes(self, service):
        first = service.import_text(SAMPLE_CHAT, "proj")
        again = service.import_text(SAMPLE_CHAT, "proj")
        assert again.package.version == 2 and again.added_items == 0
        assert again.package.memory.total_items == first.package.memory.total_items
        replaced = service.import_text("User: I decided to use Rust.", "proj", mode="replace")
        assert replaced.package.version == 3
        assert replaced.package.memory.total_items == 1
        assert replaced.package.history[-1].note == "replaced"

    @pytest.mark.parametrize("bad", ["../x", "", "bad name", "x" * 70])
    def test_rejects_bad_names(self, service, bad):
        with pytest.raises(ValidationError):
            service.import_text(SAMPLE_CHAT, bad)

    def test_rejects_empty_and_oversized_text(self, service):
        with pytest.raises(ValidationError):
            service.import_text("   ", "p")
        service.settings = service.settings.model_copy(update={"max_text_chars": 10})
        with pytest.raises(ValidationError):
            service.import_text(SAMPLE_CHAT, "p")

    def test_warns_when_nothing_extracted(self, service):
        outcome = service.import_text("Just some text with nothing recognisable.", "empty")
        assert outcome.warnings and outcome.package.memory.total_items == 0

    def test_engine_resolution_uses_adapter_factory(self, tmp_path, settings):
        calls: list[tuple] = []

        def factory(name, **kwargs):
            calls.append((name, kwargs))
            adapter = MockAdapter()
            adapter.set_response(json.dumps({"decisions": [{"content": "Use Rust"}]}))
            return adapter

        from contextbridge.storage.sqlite_store import SQLiteStore

        store = SQLiteStore(base_dir=tmp_path)
        try:
            svc = ContextBridgeService(store, settings=settings, adapter_factory=factory)
            assert svc.resolve_engine("local") == ("local", None)
            assert svc.resolve_engine("LOCAL")[1] is None
            outcome = svc.import_text(SAMPLE_CHAT, "llm", engine="openai")
            assert outcome.engine == "openai"
            assert outcome.package.memory.decisions[0].content == "Use Rust"
            svc.import_text(SAMPLE_CHAT, "oll", engine="llama3:8b")
            assert calls == [("openai", {}), ("local", {"model": "llama3:8b"})]
            assert svc.get_package("oll").source_model == "ollama:llama3:8b"
        finally:
            store.close()


class TestPackageOps:
    def test_remove_items_creates_version(self, service):
        pkg = service.import_text(SAMPLE_CHAT, "proj").package
        victim = pkg.memory.all_items[0].id
        updated = service.remove_items("proj", [victim], note="review")
        assert updated.version == 2
        assert victim not in updated.memory.items_by_id()
        assert updated.history[-1].note == "review"
        assert updated.history[-1].diff.removed[0].id == victim
        with pytest.raises(ValidationError):
            service.remove_items("proj", [])

    def test_history_and_rollback(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        service.import_text("User: I decided to use Rust.", "proj")
        hist = service.history("proj")
        assert [h["version"] for h in hist] == [1, 2]
        assert hist[1]["added"] == 1
        pkg = service.rollback("proj", 1)
        assert pkg.version == 1
        with pytest.raises(ValueError):
            service.rollback("proj", 5)

    def test_delete_and_missing(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        service.delete_package("proj")
        assert service.list_packages() == []
        with pytest.raises(PackageNotFoundError):
            service.delete_package("proj")
        with pytest.raises(PackageNotFoundError):
            service.get_package("nope")


class TestRetrievalAndPrompt:
    def test_retrieve_returns_scored_items(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        result = service.retrieve("proj", "Pydantic data models", RetrievalOptions(top_k=2))
        assert result.selected and result.selected[0].score > 0
        assert "pydantic" in result.selected[0].matched_terms
        assert result.summary()["tokens_saved_vs_full_memory"] >= 0

    def test_prompt_full_and_retrieval_narrowed(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        full = service.build_prompt("proj", "claude")
        assert "<context>" in full["prompt"] and full["retrieval"] is None
        assert full["included_items"] == full["total_items"]

        narrowed = service.build_prompt(
            "proj", "openai", query="FAISS vector search", options=RetrievalOptions(top_k=1)
        )
        assert narrowed["included_items"] == 1
        assert "Selected by lexical retrieval" in narrowed["prompt"]
        assert narrowed["tokens_prompt"] < narrowed["tokens_full_prompt"]
        assert "## Decisions Made" in narrowed["prompt"]

    def test_prompt_with_extras_and_empty(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        built = service.build_prompt("proj", "claude", extras=[("ATTACHED FILE: a.txt", "hello")])
        assert "ATTACHED FILE: a.txt" in built["prompt"] and "hello" in built["prompt"]
        service.import_text("User: nothing here", "blank")
        with pytest.raises(ValidationError):
            service.build_prompt("blank", "claude")

    def test_retrieve_with_adapter_uses_embeddings(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        adapter = MockAdapter(semantic=True)
        result = service.retrieve("proj", "FAISS", adapter=adapter)
        assert result.method == "hybrid" and adapter.embed_calls > 0


class TestPortabilityAndMerge:
    def test_export_import_roundtrip(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        text = service.export_package_json("proj")
        with pytest.raises(ValidationError):
            service.import_package_data(text)  # exists
        copy = service.import_package_data(text, rename="proj_copy")
        assert copy.name == "proj_copy"
        assert service.get_package("proj_copy").memory.total_items == copy.memory.total_items
        replaced = service.import_package_data(text, overwrite=True)
        assert replaced.name == "proj"

    def test_merge_dry_run_then_save(self, service):
        service.import_text(SAMPLE_CHAT, "a")
        service.import_text(SAMPLE_CHAT + "\n\nUser: I decided to use Rust for the CLI.", "b")
        merged, result = service.merge_packages(["a", "b"], "ab", dry_run=True)
        assert not service.store.exists("ab")
        assert result.duplicates and result.kept_items < result.input_items
        merged, _ = service.merge_packages(["a", "b"], "ab")
        assert service.store.exists("ab")
        assert merged.metadata["merged_from"][0]["name"] == "a"
        with pytest.raises(ValidationError):
            service.merge_packages(["a"], "solo")
        with pytest.raises(ValidationError):
            service.merge_packages(["a", "b"], "ab")  # exists


class TestScan:
    def test_scan_reports_without_storing(self, service):
        report = service.scan("mail me at a@b.io")
        assert report.counts == {"email": 1}
        assert service.list_packages() == []


def test_local_extractor_sets_origin():
    memory = LocalExtractor().extract(SAMPLE_CHAT, origin="x.txt")
    assert memory.total_items > 0
    assert {i.origin for i in memory.all_items} == {"x.txt"}
    assert memory.get_category(MemoryCategory.DECISIONS)
