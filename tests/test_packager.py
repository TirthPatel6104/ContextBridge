"""Tests for the Context Packager."""

import pytest

from contextbridge.core.packager import ContextPackager
from contextbridge.models import MemoryCategory, MemoryItem, StructuredMemory


@pytest.fixture
def packager():
    return ContextPackager()


@pytest.fixture
def extra_memory():
    return StructuredMemory(
        identity=[
            MemoryItem(category=MemoryCategory.IDENTITY, content="User is Tirth, a CS student")
        ],
        projects=[
            MemoryItem(category=MemoryCategory.PROJECTS, content="Building ContextBridge"),
            MemoryItem(category=MemoryCategory.PROJECTS, content="Also working on a web scraper"),
        ],
        facts=[MemoryItem(category=MemoryCategory.FACTS, content="Python 3.11 required")],
        decisions=[],
        open_tasks=[],
        preferences=[],
    )


class TestContextPackager:
    def test_create_package(self, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory, source_model="gpt")

        assert pkg.name == "test_pkg"
        assert pkg.version == 1
        assert pkg.source_model == "gpt"
        assert pkg.memory.total_items == sample_memory.total_items
        assert len(pkg.history) == 1

    def test_update_bumps_version(self, packager, sample_memory, extra_memory):
        pkg = packager.create("test_pkg", sample_memory, source_model="gpt")
        pkg = packager.update(pkg, extra_memory, source_model="claude")

        assert pkg.version == 2
        assert pkg.source_model == "claude"
        assert len(pkg.history) == 2

    def test_diff_detects_changes(self, packager, sample_memory, extra_memory):
        diff = packager.diff(sample_memory, extra_memory)

        assert len(diff.added) > 0  # extra_memory has new items
        assert len(diff.removed) > 0  # sample_memory items not in extra

    def test_rollback(self, packager, sample_memory, extra_memory):
        pkg = packager.create("test_pkg", sample_memory, source_model="gpt")
        original_items = pkg.memory.total_items

        pkg = packager.update(pkg, extra_memory, source_model="claude")
        assert pkg.version == 2

        pkg = packager.rollback(pkg, target_version=1)
        assert pkg.version == 1
        assert pkg.memory.total_items == original_items

    def test_rollback_invalid_version(self, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory)

        with pytest.raises(ValueError):
            packager.rollback(pkg, target_version=0)

        with pytest.raises(ValueError):
            packager.rollback(pkg, target_version=1)  # same as current

    def test_version_summary(self, packager, sample_memory, extra_memory):
        pkg = packager.create("test_pkg", sample_memory)
        pkg = packager.update(pkg, extra_memory)

        summaries = packager.get_version_summary(pkg)
        assert len(summaries) == 2
        assert summaries[0]["version"] == 1
        assert summaries[1]["version"] == 2
