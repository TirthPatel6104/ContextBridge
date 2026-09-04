"""Tests for storage backends."""

import pytest

from contextbridge.core.packager import ContextPackager
from contextbridge.storage.json_store import JSONStore


@pytest.fixture
def store(tmp_path):
    return JSONStore(base_dir=tmp_path)


@pytest.fixture
def packager():
    return ContextPackager()


class TestJSONStore:
    def test_save_and_load(self, store, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory, source_model="gpt")
        store.save(pkg)

        loaded = store.load("test_pkg")
        assert loaded.name == "test_pkg"
        assert loaded.version == 1
        assert loaded.memory.total_items == sample_memory.total_items

    def test_load_specific_version(self, store, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory)
        store.save(pkg)

        # Update and save v2
        from contextbridge.models import MemoryCategory, MemoryItem, StructuredMemory

        new_memory = StructuredMemory(
            identity=[MemoryItem(category=MemoryCategory.IDENTITY, content="Updated identity")],
        )
        pkg = packager.update(pkg, new_memory)
        store.save(pkg)

        # Load v1
        v1 = store.load("test_pkg", version=1)
        assert v1.version == 1
        assert v1.memory.total_items == sample_memory.total_items

        # Load latest (v2)
        latest = store.load("test_pkg")
        assert latest.version == 2

    def test_list_packages(self, store, packager, sample_memory):
        assert store.list_packages() == []

        pkg1 = packager.create("alpha", sample_memory)
        pkg2 = packager.create("beta", sample_memory)
        store.save(pkg1)
        store.save(pkg2)

        packages = store.list_packages()
        assert "alpha" in packages
        assert "beta" in packages

    def test_delete(self, store, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory)
        store.save(pkg)
        assert store.exists("test_pkg")

        store.delete("test_pkg")
        assert not store.exists("test_pkg")

    def test_load_nonexistent(self, store):
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent")

    def test_exists(self, store, packager, sample_memory):
        assert not store.exists("test_pkg")

        pkg = packager.create("test_pkg", sample_memory)
        store.save(pkg)
        assert store.exists("test_pkg")

    def test_list_versions(self, store, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory)
        store.save(pkg)

        from contextbridge.models import StructuredMemory

        pkg = packager.update(pkg, StructuredMemory())
        store.save(pkg)

        versions = store.list_versions("test_pkg")
        assert versions == [1, 2]
