"""Tests for sharing boundaries, the egress ledger, memory lifecycle, and hand-offs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from contextbridge.core.lifecycle import (
    absorb,
    detect_handoff,
    find_pending_conflicts,
    health_report,
    set_status,
    supersede,
)
from contextbridge.core.packager import ContextPackager
from contextbridge.core.prompt_builder import PromptBuilder
from contextbridge.core.retriever import MemoryRetriever
from contextbridge.core.sharing import (
    apply_sharing,
    classify_target,
    partition_for_target,
    sharing_counts,
)
from contextbridge.models import (
    EgressRecord,
    ItemStatus,
    MemoryCategory,
    MemoryItem,
    RetrievalOptions,
    SharingPolicy,
    StructuredMemory,
)
from contextbridge.storage.json_store import JSONStore
from contextbridge.storage.sqlite_store import SQLiteStore
from contextbridge.validation import ValidationError
from tests.conftest import SAMPLE_CHAT

# ---------------------------------------------------------------------------
# Sharing boundaries (pure)
# ---------------------------------------------------------------------------


class TestSharing:
    @pytest.mark.parametrize(
        "target,kind",
        [
            ("claude", "cloud"),
            ("openai", "cloud"),
            ("gpt-4o", "cloud"),
            ("", "cloud"),
            ("something-new", "cloud"),
            ("local", "local"),
            ("ollama", "local"),
            ("llama3:8b", "local"),
        ],
    )
    def test_classify_target_defaults_to_cloud(self, target, kind):
        assert classify_target(target) == kind

    def test_partition_withholds_by_policy_and_status(self, sample_memory):
        items = sample_memory.all_items
        memory, changed = apply_sharing(sample_memory, {items[0].id}, SharingPolicy.LOCAL_ONLY)
        memory, changed2 = apply_sharing(memory, {items[1].id}, SharingPolicy.NEVER)
        memory, _ = set_status(memory, {items[2].id}, ItemStatus.DONE)
        assert changed == 1 and changed2 == 1

        allowed, withheld = partition_for_target(memory, "claude")
        reasons = {w.item.id: w.reason for w in withheld}
        assert set(reasons) == {items[0].id, items[1].id, items[2].id}
        assert "local models only" in reasons[items[0].id]
        assert "never" in reasons[items[1].id]
        assert reasons[items[2].id] == "marked done"
        assert allowed.total_items == memory.total_items - 3

        allowed_local, withheld_local = partition_for_target(memory, "ollama")
        assert {w.item.id for w in withheld_local} == {items[1].id, items[2].id}
        assert items[0].id in allowed_local.items_by_id()

    def test_apply_sharing_is_idempotent_and_counts(self, sample_memory):
        ids = [i.id for i in sample_memory.all_items[:2]]
        memory, changed = apply_sharing(sample_memory, ids, SharingPolicy.NEVER)
        assert changed == 2
        _, again = apply_sharing(memory, ids, SharingPolicy.NEVER)
        assert again == 0
        assert sharing_counts(memory)["never"] == 2


# ---------------------------------------------------------------------------
# Lifecycle (pure)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_supersede_marks_old_and_validates(self, sample_memory):
        a, b = sample_memory.decisions
        memory = supersede(sample_memory, a.id, b.id)
        old = memory.items_by_id()[a.id]
        assert old.status == ItemStatus.SUPERSEDED and old.superseded_by == b.id
        assert memory.active().total_items == sample_memory.total_items - 1
        with pytest.raises(ValueError):
            supersede(memory, b.id, a.id)  # a is superseded: cannot be a replacement
        with pytest.raises(ValueError):
            supersede(memory, b.id, b.id)
        with pytest.raises(KeyError):
            supersede(memory, "nope", b.id)

    def test_reactivate_clears_pointer(self, sample_memory):
        a, b = sample_memory.decisions
        memory = supersede(sample_memory, a.id, b.id)
        memory, changed = set_status(memory, {a.id}, ItemStatus.ACTIVE)
        assert changed == 1
        assert memory.items_by_id()[a.id].superseded_by == ""

    def test_absorb_counts_sightings_and_keeps_status(self, sample_memory):
        a = sample_memory.decisions[0]
        stored, _ = set_status(sample_memory, {a.id}, ItemStatus.DONE)
        incoming = StructuredMemory.from_items(
            [
                MemoryItem(category=a.category, content=a.content, origin="second.txt"),
                MemoryItem(category=MemoryCategory.FACTS, content="Brand new fact"),
            ]
        )
        later = datetime.now(UTC) + timedelta(days=1)
        merged, added, re_seen = absorb(stored, incoming, now=later)
        assert (added, re_seen) == (1, 1)
        seen = merged.items_by_id()[a.id]
        assert seen.seen_count == 2 and seen.last_seen == later
        assert seen.status == ItemStatus.DONE  # a re-statement does not resurrect it
        assert "second.txt" in seen.origin

    def test_pending_conflicts_only_cross_boundary(self):
        stored = StructuredMemory.from_items(
            [
                MemoryItem(
                    category=MemoryCategory.DECISIONS,
                    content="Using FAISS for vector search in the retrieval layer",
                    origin="a.txt",
                ),
                MemoryItem(
                    category=MemoryCategory.DECISIONS,
                    content="Using Qdrant for vector search in the retrieval layer",
                    origin="a.txt",
                ),
            ]
        )
        incoming = StructuredMemory.from_items(
            [
                MemoryItem(
                    category=MemoryCategory.DECISIONS,
                    content="Switch to pgvector for vector search in the retrieval layer",
                    origin="b.txt",
                )
            ]
        )
        pending = find_pending_conflicts(stored, incoming)
        # Both stored items conflict with the new one; the two stored items are
        # never compared with each other.
        assert {p.existing_id for p in pending} == {i.id for i in stored.all_items}
        assert all(p.incoming_id == incoming.all_items[0].id for p in pending)
        assert all("supersedes" in p.reason for p in pending)

    def test_detect_handoff_strips_pasted_prompt(self, sample_memory):
        pkg = ContextPackager().create("src", sample_memory, source_model="gpt")
        builder = PromptBuilder()
        pasted = builder.paste_prompt(builder.build(pkg, "claude"), "claude")
        transcript = (
            "User: " + pasted + "\n\nAssistant: Understood.\n\n"
            "User: I decided to use Rust for the CLI.\n\nAssistant: Great."
        )
        stripped, handoff = detect_handoff(transcript)
        assert handoff is not None
        assert (handoff.package_name, handoff.package_version) == ("src", 1)
        assert handoff.stripped_chars > 100
        assert "<context>" not in stripped and "Rust for the CLI" in stripped
        assert detect_handoff("User: hello") == ("User: hello", None)

    def test_detect_handoff_footer_only(self):
        text = 'Some notes.\nContext: "proj" v3 | Source: gpt | Items: 4\nMore notes.'
        stripped, handoff = detect_handoff(text)
        assert handoff and handoff.package_name == "proj" and handoff.package_version == 3
        assert stripped == text and handoff.stripped_chars == 0

    def test_health_report_counts(self, sample_memory):
        pkg = ContextPackager().create("h", sample_memory)
        old = datetime.now(UTC) - timedelta(days=60)
        stale_item = pkg.memory.all_items[0]
        stale_item.last_seen = old
        ledger = [
            EgressRecord(
                package_name="h",
                package_version=1,
                target_model="claude",
                target_kind="cloud",
                item_ids=[stale_item.id],
                item_count=1,
            )
        ]
        report = health_report(pkg, egress=ledger, stale_days=30)
        c = report["counts"]
        assert c["stale"] == 1 and report["stale_items"][0]["id"] == stale_item.id
        assert c["shared_with_cloud"] == 1
        assert c["never_shared"] == sample_memory.total_items - 1
        assert c["open_tasks"] == 1
        assert health_report(pkg, stale_days=0)["counts"]["stale"] == 0


# ---------------------------------------------------------------------------
# Packager: tracked-field diffs and rollback
# ---------------------------------------------------------------------------


class TestPackagerLifecycle:
    def test_apply_records_modified_and_rollback_restores(self, sample_memory):
        packager = ContextPackager()
        pkg = packager.create("p", sample_memory)
        target = pkg.memory.all_items[0]
        memory, _ = apply_sharing(pkg.memory, {target.id}, SharingPolicy.NEVER)
        pkg = packager.apply(pkg, memory, note="sharing")
        assert pkg.version == 2
        entry = pkg.history[-1]
        assert not entry.diff.added and not entry.diff.removed
        assert entry.diff.modified == [
            {
                "id": target.id,
                "category": target.category.value,
                "before": {"status": "active", "superseded_by": "", "sharing": "any"},
                "after": {"status": "active", "superseded_by": "", "sharing": "never"},
            }
        ]
        assert packager.get_version_summary(pkg)[-1]["modified"] == 1

        pkg = packager.rollback(pkg, 1)
        restored = pkg.memory.items_by_id()[target.id]
        assert restored.sharing is SharingPolicy.ANY  # an enum again, not a string
        assert restored.status is ItemStatus.ACTIVE

    def test_apply_is_noop_without_changes(self, sample_memory):
        packager = ContextPackager()
        pkg = packager.create("p", sample_memory)
        assert packager.apply(pkg, pkg.memory.model_copy(deep=True), note="x").version == 1


class TestRetrieverLifecycle:
    def test_inactive_items_are_excluded_unless_asked(self, sample_memory):
        a, b = sample_memory.decisions
        memory = supersede(sample_memory, a.id, b.id)
        retriever = MemoryRetriever()
        retriever.index_sync(memory)
        result = retriever.retrieve_sync("Pydantic data models", RetrievalOptions(top_k=5))
        assert a.id not in {s.item.id for s in result.selected}
        assert result.excluded_inactive == 1
        assert result.summary()["excluded_inactive"] == 1
        wide = retriever.retrieve_sync(
            "Pydantic data models", RetrievalOptions(top_k=5, include_inactive=True)
        )
        assert a.id in {s.item.id for s in wide.selected}


# ---------------------------------------------------------------------------
# Storage: egress ledger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_ledger_roundtrip_and_delete(tmp_path, sample_memory, backend):
    store = SQLiteStore(base_dir=tmp_path) if backend == "sqlite" else JSONStore(base_dir=tmp_path)
    try:
        pkg = ContextPackager().create("p", sample_memory)
        store.save(pkg)
        first = store.record_egress(
            EgressRecord(
                package_name="p",
                package_version=1,
                target_model="claude",
                target_kind="cloud",
                surface="cli",
                query="schema",
                item_ids=["a", "b"],
                item_count=2,
                withheld_count=1,
                tokens=42,
            )
        )
        second = store.record_egress(
            EgressRecord(
                package_name="p",
                package_version=1,
                target_model="ollama",
                target_kind="local",
                item_ids=["a"],
                item_count=1,
            )
        )
        assert first.id and second.id and second.id > first.id
        rows = store.egress_records("p")
        assert [r.id for r in rows] == [second.id, first.id]  # newest first
        assert rows[1].item_ids == ["a", "b"] and rows[1].withheld_count == 1
        assert rows[1].tokens == 42 and rows[1].query == "schema"
        assert store.egress_records("other") == []
        assert store.egress_records("../bad") == []
        if backend == "sqlite":
            assert store.stats()["egress_events"] == 2
        assert store.clear_egress("p") == 2
        assert store.egress_records("p") == []
        store.record_egress(second.model_copy(update={"id": None}))
        store.delete("p")
        assert store.egress_records("p") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Service workflows
# ---------------------------------------------------------------------------


class TestServiceTrust:
    def _ids(self, service, name):
        return [i.id for i in service.get_package(name).memory.all_items]

    def test_local_only_is_withheld_from_cloud_and_recorded(self, service):
        service.import_text(SAMPLE_CHAT, "proj", origin="chat.txt")
        pkg = service.get_package("proj")
        secret = next(i for i in pkg.memory.all_items if "Pydantic" in i.content)
        pkg, changed = service.set_sharing("proj", [secret.id], "local_only")
        assert changed == 1 and pkg.version == 2
        assert pkg.history[-1].note == "sharing set to local_only"

        cloud = service.build_prompt("proj", "claude", surface="cli")
        assert "Pydantic" not in cloud["prompt"]
        assert cloud["withheld_count"] == 1
        assert cloud["withheld"][0].item.id == secret.id
        assert "Withheld: 1" in cloud["prompt"]
        assert cloud["target_kind"] == "cloud" and cloud["egress_id"] == 1

        local = service.build_prompt("proj", "ollama", surface="cli")
        assert "Pydantic" in local["prompt"] and local["withheld_count"] == 0

        audit = service.audit("proj")
        assert audit["summary"]["events"] == 2
        assert audit["summary"]["targets"] == ["claude", "ollama"]
        shared = {i["id"]: i for i in audit["items"]}
        assert shared[secret.id]["sent_to_cloud"] is False
        assert shared[secret.id]["targets"] == {"ollama": 1}
        other = next(i for i in audit["items"] if i["id"] != secret.id)
        assert other["sent_to_cloud"] is True and other["targets"] == {"claude": 1, "ollama": 1}
        assert audit["summary"]["items_shared_with_cloud"] == pkg.memory.total_items - 1

    def test_never_and_preview_do_not_record(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        ids = self._ids(service, "proj")
        service.set_sharing("proj", ids, "never")
        with pytest.raises(ValidationError, match="withheld"):
            service.build_prompt("proj", "ollama")
        service.set_sharing("proj", ids[:1], "any")
        built = service.build_prompt("proj", "claude", record=False)
        assert built["egress_id"] is None and built["included_items"] == 1
        assert service.audit("proj")["summary"]["events"] == 0
        with pytest.raises(ValidationError):
            service.set_sharing("proj", ids[:1], "everyone")
        with pytest.raises(ValidationError, match="Unknown item"):
            service.set_sharing("proj", ["nope"], "any")

    def test_retrieval_scoped_prompt_only_ranks_allowed_items(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        pkg = service.get_package("proj")
        faiss = next(i for i in pkg.memory.all_items if "FAISS" in i.content)
        service.set_sharing("proj", [faiss.id], "never")
        with pytest.raises(ValidationError, match="matching the query"):
            # The only FAISS item is hidden, so a FAISS-only query finds nothing.
            service.build_prompt("proj", "openai", query="FAISS", options=RetrievalOptions(top_k=3))
        built = service.build_prompt(
            "proj", "openai", query="Python FastAPI", options=RetrievalOptions(top_k=10)
        )
        assert faiss.id not in built["included_ids"]
        assert built["retrieval"].total_items == pkg.memory.total_items - 1
        assert all(w.item.id == faiss.id for w in built["withheld"])

    def test_done_tasks_leave_prompts_and_rollback_undoes(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        task = service.get_package("proj").memory.open_tasks[0]
        pkg, changed = service.set_status("proj", [task.id], "done")
        assert changed == 1 and pkg.history[-1].note == "marked done"
        assert task.id not in service.build_prompt("proj", "claude")["included_ids"]
        assert service.health("proj")["counts"]["status_done"] == 1
        pkg = service.rollback("proj", 1)
        assert pkg.memory.items_by_id()[task.id].status is ItemStatus.ACTIVE
        with pytest.raises(ValidationError):
            service.set_status("proj", [task.id], "superseded")
        pkg, _ = service.set_status("proj", [task.id], "done")
        pkg, changed = service.set_status("proj", [task.id], "active")
        assert changed == 1 and pkg.history[-1].note == "reactivated"

    def test_supersede_via_service(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        a, b = service.get_package("proj").memory.decisions[:2]
        pkg = service.supersede("proj", a.id, b.id)
        assert pkg.memory.items_by_id()[a.id].superseded_by == b.id
        built = service.build_prompt("proj", "claude")
        assert a.id not in built["included_ids"] and b.id in built["included_ids"]
        with pytest.raises(ValidationError):
            service.supersede("proj", b.id, a.id)
        with pytest.raises(ValidationError, match="Unknown item"):
            service.supersede("proj", "zzz", b.id)

    def test_import_detects_conflicts_and_handoff(self, service):
        first = service.import_text(SAMPLE_CHAT, "proj", origin="chatgpt.txt")
        pkg = first.package
        assert not first.conflicts and first.handoff is None

        # Simulate pasting the prompt into Claude and importing that chat back.
        built = service.build_prompt("proj", "claude")
        followup = (
            "User: " + built["prompt"] + "\n\n"
            "Assistant: Got it, I have the context.\n\n"
            "User: Actually, I decided to use Qdrant instead of FAISS for vector search.\n\n"
            "Assistant: Noted."
        )
        second = service.import_text(followup, "proj", origin="claude.txt")
        assert second.handoff is not None
        assert second.handoff.package_name == "proj" and second.handoff.package_version == 1
        assert second.handoff.stripped_chars > 0
        assert any("Hand-off detected" in n for n in second.notes)
        # The pasted block was not re-extracted: only the new decision was added.
        assert second.added_items == 1
        assert second.re_seen_items == 0
        assert second.package.metadata["handoffs"][0]["origin"] == "claude.txt"

        pending = service.pending_conflicts("proj")
        assert len(pending) == 1 and second.conflicts[0].key == pending[0].key
        faiss = next(i for i in pkg.memory.all_items if "FAISS" in i.content)
        new = next(i for i in second.package.memory.all_items if "Qdrant" in i.content)
        assert (pending[0].existing_id, pending[0].incoming_id) == (faiss.id, new.id)
        assert "supersedes" in pending[0].reason

        detail = service.package_to_dict(service.get_package("proj"))
        assert detail["pending_conflicts"][0]["existing"]["id"] == faiss.id

        resolved = service.resolve_conflict("proj", faiss.id, new.id, "keep_new")
        assert resolved.memory.items_by_id()[faiss.id].superseded_by == new.id
        assert service.pending_conflicts("proj") == []
        with pytest.raises(ValidationError, match="No such pending"):
            service.resolve_conflict("proj", faiss.id, new.id, "dismiss")
        with pytest.raises(ValidationError):
            service.resolve_conflict("proj", faiss.id, new.id, "explode")

    def test_conflicts_dismiss_and_keep_old(self, service):
        service.import_text(
            "User: I decided to use FAISS for vector search in the retrieval layer.",
            "proj",
            origin="a.txt",
        )
        out = service.import_text(
            "User: I decided to switch to pgvector for vector search in the retrieval layer.",
            "proj",
            origin="b.txt",
        )
        assert len(out.conflicts) == 1
        c = out.conflicts[0]
        service.resolve_conflict("proj", c.existing_id, c.incoming_id, "keep_old")
        pkg = service.get_package("proj")
        assert pkg.memory.items_by_id()[c.incoming_id].superseded_by == c.existing_id
        # Re-importing b.txt re-flags nothing: the incoming item already exists.
        again = service.import_text(
            "User: I decided to switch to pgvector for vector search in the retrieval layer.",
            "proj",
            origin="b.txt",
        )
        assert again.conflicts == [] and again.re_seen_items == 1
        assert pkg.memory.items_by_id()[c.incoming_id].status is ItemStatus.SUPERSEDED

    def test_re_import_corroborates(self, service):
        service.import_text(SAMPLE_CHAT, "proj", origin="one.txt")
        out = service.import_text(SAMPLE_CHAT, "proj", origin="two.txt")
        assert out.added_items == 0 and out.re_seen_items == out.extracted.total_items
        item = service.get_package("proj").memory.all_items[0]
        assert item.seen_count == 2 and item.origin == "one.txt | two.txt"
        assert service.health("proj")["counts"]["corroborated"] == out.extracted.total_items

    def test_handoff_only_transcript_is_rejected(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        pasted = service.build_prompt("proj", "claude", record=False)["prompt"]
        with pytest.raises(ValidationError, match="only a pasted"):
            service.import_text(pasted, "proj")

    def test_removing_items_prunes_conflicts_and_audit_survives(self, service):
        service.import_text("User: I decided to use FAISS for vector search.", "proj")
        out = service.import_text("User: I decided to switch to Qdrant for vector search.", "proj")
        assert out.conflicts
        service.build_prompt("proj", "claude")
        service.remove_items("proj", [out.conflicts[0].incoming_id])
        assert service.pending_conflicts("proj") == []
        audit = service.audit("proj")
        assert audit["summary"]["events"] == 1
        gone = next(i for i in audit["items"] if not i["present"])
        assert gone["id"] == out.conflicts[0].incoming_id
        assert service.clear_audit("proj") == 1

    def test_query_context_is_partitioned_and_recorded(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        ids = self._ids(service, "proj")
        service.set_sharing("proj", ids[:1], "local_only")
        block, info = service.system_prompt_for_query("proj", "FAISS", "claude", smart=False)
        assert len(info["withheld"]) == 1 and info["included_items"] == len(ids) - 1
        events = service.audit("proj")["events"]
        assert events[0]["surface"] == "query" and events[0]["withheld_count"] == 1

    def test_export_roundtrip_keeps_policy_and_status(self, service):
        service.import_text(SAMPLE_CHAT, "proj")
        ids = self._ids(service, "proj")
        service.set_sharing("proj", ids[:1], "never")
        service.set_status("proj", ids[1:2], "done")
        text = service.export_package_json("proj")
        data = json.loads(text)
        assert data["schema_version"] == 3
        copy = service.import_package_data(text, rename="copy")
        by_id = copy.memory.items_by_id()
        assert by_id[ids[0]].sharing is SharingPolicy.NEVER
        assert by_id[ids[1]].status is ItemStatus.DONE

    def test_schema_v2_items_get_defaults(self, service):
        legacy = {
            "name": "old",
            "schema_version": 2,
            "version": 1,
            "memory": {
                "decisions": [
                    {
                        "id": "abc",
                        "category": "decisions",
                        "content": "Use Rust",
                        "origin": "x.txt",
                    }
                ]
            },
        }
        pkg = service.import_package_data(legacy)
        item = pkg.memory.decisions[0]
        assert item.sharing is SharingPolicy.ANY and item.status is ItemStatus.ACTIVE
        assert item.seen_count == 1 and item.first_seen is not None
        assert service.build_prompt("old", "claude")["included_items"] == 1


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------


class TestWebTrust:
    def _upload(self, client, name="proj", text=SAMPLE_CHAT, filename="chat.txt"):
        import io

        data = {"package_name": name, "model": "local"}
        data["files[]"] = (io.BytesIO(text.encode("utf-8")), filename)
        return client.post("/api/extract", data=data, content_type="multipart/form-data")

    def test_health_advertises_policies(self, client):
        data = client.get("/api/health").get_json()
        assert data["sharing_policies"] == ["any", "local_only", "never"]
        assert data["item_statuses"] == ["active", "superseded", "done"]

    def test_sharing_status_prompt_and_audit(self, client):
        detail = self._upload(client).get_json()
        items = [i for cat in detail["memory"].values() for i in cat]
        assert all(i["sharing"] == "any" and i["status"] == "active" for i in items)
        first, second = items[0], items[1]

        res = client.post(
            "/api/packages/proj/items/sharing", json={"ids": [first["id"]], "policy": "local_only"}
        )
        assert res.status_code == 200 and res.get_json()["changed"] == 1
        bad = client.post(
            "/api/packages/proj/items/sharing", json={"ids": [first["id"]], "policy": "loud"}
        )
        assert bad.status_code == 400
        res = client.post(
            "/api/packages/proj/items/status", json={"ids": [second["id"]], "status": "done"}
        )
        assert res.status_code == 200 and res.get_json()["version"] == 3

        pkg = client.get("/api/packages/proj").get_json()
        assert pkg["status_counts"]["done"] == 1 and pkg["sharing_counts"]["local_only"] == 1
        assert pkg["active_items"] == pkg["total_items"] - 1
        assert pkg["history"][-1]["modified"] == 1

        prompt = client.post(
            "/api/prompt",
            json={"package_name": "proj", "target_model": "claude", "surface": "dashboard"},
        ).get_json()
        assert prompt["withheld_count"] == 2
        reasons = {w["id"]: w["reason"] for w in prompt["withheld"]}
        assert "local" in reasons[first["id"]] and reasons[second["id"]] == "marked done"
        assert prompt["recorded"] is True and prompt["egress_id"] == 1
        assert first["id"] not in prompt["included_ids"]

        preview = client.post(
            "/api/prompt",
            json={"package_name": "proj", "target_model": "local", "record": "false"},
        ).get_json()
        assert preview["recorded"] is False and preview["withheld_count"] == 1

        audit = client.get("/api/packages/proj/audit").get_json()
        assert audit["summary"]["events"] == 1
        assert audit["events"][0]["surface"] == "dashboard"
        assert audit["events"][0]["target_kind"] == "cloud"
        health = client.get("/api/packages/proj/health?stale_days=0").get_json()
        assert health["counts"]["shared_with_cloud"] == audit["summary"]["items_shared"]
        assert client.delete("/api/packages/proj/audit").get_json()["removed"] == 1
        assert client.get("/api/packages/nope/audit").status_code == 404

    def test_supersede_and_conflicts_endpoints(self, client):
        self._upload(client, text="User: I decided to use FAISS for vector search.")
        data = self._upload(
            client, text="User: I decided to switch to Qdrant for vector search.", filename="b.txt"
        ).get_json()
        assert data["handoff"] is None and len(data["conflicts"]) == 1
        c = data["conflicts"][0]
        listing = client.get("/api/packages/proj/conflicts").get_json()["conflicts"]
        assert (
            listing[0]["existing"]["content"].startswith("Using FAISS")
            or listing[0]["existing"]["id"] == c["existing_id"]
        )
        res = client.post(
            "/api/packages/proj/conflicts/resolve",
            json={
                "existing_id": c["existing_id"],
                "incoming_id": c["incoming_id"],
                "action": "dismiss",
            },
        )
        assert res.status_code == 200 and res.get_json()["remaining"] == 0
        res = client.post(
            "/api/packages/proj/items/supersede",
            json={"old_id": c["existing_id"], "new_id": c["incoming_id"]},
        )
        assert res.status_code == 200
        pkg = client.get("/api/packages/proj").get_json()
        old = next(i for cat in pkg["memory"].values() for i in cat if i["id"] == c["existing_id"])
        assert old["status"] == "superseded" and old["superseded_by"] == c["incoming_id"]
        assert client.post("/api/packages/proj/items/supersede", json={}).status_code == 400

    def test_extract_reports_handoff(self, client):
        self._upload(client)
        prompt = client.post(
            "/api/prompt", json={"package_name": "proj", "target_model": "claude", "record": False}
        ).get_json()["prompt"]
        followup = "User: " + prompt + "\n\nAssistant: ok\n\nUser: I decided to use Rust."
        data = self._upload(client, text=followup, filename="claude.txt").get_json()
        assert data["handoff"]["package_name"] == "proj"
        assert data["added_items"] == 1
        assert any("Hand-off" in n for n in data["notes"])
        assert client.get("/api/packages/proj").get_json()["handoffs"][0]["origin"] == "claude.txt"
