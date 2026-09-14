"""Tests for multi-package merging, duplicate folding, and conflict flags."""

from __future__ import annotations

import pytest

from contextbridge.core.merger import PackageMerger, similarity
from contextbridge.core.packager import ContextPackager
from contextbridge.models import MemoryCategory, MemoryItem, StructuredMemory


def _mem(*pairs: tuple[MemoryCategory, str], origin: str = "", confidence: float = 1.0):
    return StructuredMemory.from_items(
        [MemoryItem(category=c, content=t, origin=origin, confidence=confidence) for c, t in pairs]
    )


class TestSimilarity:
    def test_identical_and_disjoint(self):
        assert similarity("Use PostgreSQL", "use  postgresql") == 1.0
        assert similarity("Use PostgreSQL", "Prefers dark mode") < 0.3

    def test_symmetric(self):
        a, b = "Write the Alembic migration", "Write an Alembic migration for the ledger"
        assert similarity(a, b) == similarity(b, a)


class TestPackageMerger:
    def test_exact_duplicates_fold_and_keep_origins(self):
        merger = PackageMerger()
        a = _mem((MemoryCategory.DECISIONS, "Use PostgreSQL for production"), origin="chat_a")
        b = _mem((MemoryCategory.DECISIONS, "Use PostgreSQL for production."), origin="chat_b")
        result = merger.merge_memories([("chat_a", a), ("chat_b", b)])
        assert result.input_items == 2 and result.kept_items == 1
        assert len(result.duplicates) == 1
        kept = result.memory.decisions[0]
        assert kept.origin == "chat_a | chat_b"
        assert result.duplicates[0].origins == ["chat_a", "chat_b"]
        assert result.conflicts == []

    def test_keeps_most_confident_phrasing(self):
        merger = PackageMerger()
        a = _mem((MemoryCategory.FACTS, "Deadline is the end of Q3"), confidence=0.6)
        b = _mem((MemoryCategory.FACTS, "The deadline is the end of Q3"), confidence=0.9)
        result = merger.merge_memories([("a", a), ("b", b)])
        assert result.memory.facts[0].content == "The deadline is the end of Q3"

    def test_conflicting_statements_are_flagged_not_dropped(self):
        merger = PackageMerger()
        a = _mem((MemoryCategory.FACTS, "Deadline is the end of Q3"))
        b = _mem((MemoryCategory.FACTS, "Deadline is the end of Q4"))
        result = merger.merge_memories([("a", a), ("b", b)])
        assert result.kept_items == 2
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.category == MemoryCategory.FACTS
        assert "deadline" in conflict.shared_terms
        assert conflict.reason

    def test_negation_conflict_reason(self):
        merger = PackageMerger()
        a = _mem((MemoryCategory.PREFERENCES, "Avoids closed-source APIs for experiments"))
        b = _mem((MemoryCategory.PREFERENCES, "Uses closed-source APIs for experiments"))
        result = merger.merge_memories([("a", a), ("b", b)])
        assert result.conflicts and "negates" in result.conflicts[0].reason

    def test_same_text_different_categories_not_merged(self):
        merger = PackageMerger()
        a = _mem((MemoryCategory.FACTS, "Uses Python 3.11"))
        b = _mem((MemoryCategory.PREFERENCES, "Uses Python 3.11"))
        result = merger.merge_memories([("a", a), ("b", b)])
        assert result.kept_items == 2 and result.duplicates == []

    def test_open_tasks_never_flag_conflicts(self):
        merger = PackageMerger()
        a = _mem((MemoryCategory.OPEN_TASKS, "Write the migration for the ledger table"))
        b = _mem((MemoryCategory.OPEN_TASKS, "Write the migration for the accounts table"))
        result = merger.merge_memories([("a", a), ("b", b)])
        assert result.conflicts == []

    def test_merge_packages_records_provenance(self, sample_memory):
        packager = ContextPackager()
        p1 = packager.create("one", sample_memory, source_model="openai")
        p2 = packager.create(
            "two",
            _mem((MemoryCategory.DECISIONS, "Using Pydantic for data models")),
            source_model="claude",
        )
        merged, result = PackageMerger().merge_packages([p1, p2], name="both", packager=packager)
        assert merged.name == "both"
        assert merged.source_model == "claude+openai"
        assert [m["name"] for m in merged.metadata["merged_from"]] == ["one", "two"]
        assert merged.metadata["merge_report"]["duplicate_groups"] == 1
        assert result.summary()["sources"] == ["one", "two"]
        pydantic_item = next(i for i in merged.memory.decisions if "Pydantic" in i.content)
        assert pydantic_item.origin == "one | two"

    def test_threshold_validation(self):
        with pytest.raises(ValueError):
            PackageMerger(duplicate_threshold=0.3, conflict_threshold=0.5)
