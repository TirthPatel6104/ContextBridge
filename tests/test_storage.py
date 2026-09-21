"""Tests for storage backends, migration, and portable packages."""

from __future__ import annotations

import json

import pytest

from contextbridge.core.packager import ContextPackager
from contextbridge.models import (
    ContextPackage,
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)
from contextbridge.storage import get_store, import_legacy_json
from contextbridge.storage.base import PackageNotFoundError
from contextbridge.storage.json_store import JSONStore
from contextbridge.storage.portable import dumps_package, import_package
from contextbridge.storage.sqlite_store import SQLiteStore
from contextbridge.validation import ValidationError


@pytest.fixture(params=["json", "sqlite"])
def store(request, tmp_path):
    if request.param == "json":
        yield JSONStore(base_dir=tmp_path)
    else:
        s = SQLiteStore(base_dir=tmp_path)
        yield s
        s.close()


@pytest.fixture
def packager():
    return ContextPackager()


class TestStorageBackends:
    def test_save_and_load(self, store, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory, source_model="gpt")
        store.save(pkg)

        loaded = store.load("test_pkg")
        assert loaded.name == "test_pkg"
        assert loaded.version == 1
        assert loaded.memory.total_items == sample_memory.total_items
        assert loaded.memory.all_items[0].id  # ids persisted

    def test_load_specific_version(self, store, packager, sample_memory):
        pkg = packager.create("test_pkg", sample_memory)
        store.save(pkg)
        new_memory = StructuredMemory(
            identity=[MemoryItem(category=MemoryCategory.IDENTITY, content="Updated identity")],
        )
        pkg = packager.update(pkg, new_memory)
        store.save(pkg)

        v1 = store.load("test_pkg", version=1)
        assert v1.version == 1
        assert v1.memory.total_items == sample_memory.total_items
        assert store.load("test_pkg").version == 2
        assert store.list_versions("test_pkg") == [1, 2]

    def test_list_and_summaries(self, store, packager, sample_memory):
        assert store.list_packages() == []
        store.save(packager.create("alpha", sample_memory, source_model="gpt"))
        store.save(packager.create("beta", sample_memory))
        assert store.list_packages() == ["alpha", "beta"]
        summaries = {s["name"]: s for s in store.summaries()}
        assert summaries["alpha"]["total_items"] == sample_memory.total_items
        assert summaries["alpha"]["source_model"] == "gpt"
        assert summaries["beta"]["source_model"] == "unknown"

    def test_delete(self, store, packager, sample_memory):
        store.save(packager.create("test_pkg", sample_memory))
        assert store.exists("test_pkg")
        store.delete("test_pkg")
        assert not store.exists("test_pkg")
        assert store.list_versions("test_pkg") == []

    def test_load_nonexistent(self, store):
        with pytest.raises(PackageNotFoundError):
            store.load("nonexistent")
        with pytest.raises(FileNotFoundError):  # backwards compatible
            store.load("nonexistent")

    def test_missing_version(self, store, packager, sample_memory):
        store.save(packager.create("test_pkg", sample_memory))
        with pytest.raises(PackageNotFoundError):
            store.load("test_pkg", version=9)

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", "  ", "..", "x" * 65, "-lead"])
    def test_invalid_names_rejected(self, store, packager, sample_memory, bad):
        with pytest.raises(ValidationError):
            store.save(packager.create(bad, sample_memory))
        assert not store.exists(bad)
        assert store.list_versions(bad) == []
        with pytest.raises((ValidationError, PackageNotFoundError)):
            store.load(bad)

    def test_rollback_roundtrip(self, store, packager, sample_memory):
        pkg = packager.create("rb", sample_memory)
        store.save(pkg)
        pkg = packager.update(pkg, StructuredMemory())
        store.save(pkg)
        pkg = packager.rollback(pkg, 1)
        store.save(pkg)
        assert store.load("rb").version == 1
        assert store.load("rb").memory.total_items == sample_memory.total_items


class TestLegacyCompatibility:
    def test_loads_schema_v1_package_without_ids(self, tmp_path):
        """Packages written by 0.1.x have no id/origin/schema_version fields."""
        legacy = {
            "name": "old",
            "version": 1,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "source_model": "openai",
            "memory": {
                "identity": [{"category": "identity", "content": "Old user", "confidence": 1.0}],
                "projects": [],
                "facts": [],
                "decisions": [],
                "open_tasks": [],
                "preferences": [],
            },
            "history": [{"version": 1, "timestamp": "2024-01-01T00:00:00Z", "diff": {}}],
            "metadata": {},
        }
        pkg_dir = tmp_path / "old"
        pkg_dir.mkdir()
        (pkg_dir / "context_v1.json").write_text(json.dumps(legacy), encoding="utf-8")
        (pkg_dir / "latest.json").write_text(json.dumps(legacy), encoding="utf-8")

        loaded = JSONStore(base_dir=tmp_path).load("old")
        assert loaded.schema_version == 3  # default applied
        assert loaded.memory.identity[0].id
        assert loaded.memory.identity[0].origin == ""

    def test_sqlite_factory_imports_legacy_json(self, tmp_path, sample_memory):
        packager = ContextPackager()
        json_store = JSONStore(base_dir=tmp_path)
        pkg = packager.create("legacy_pkg", sample_memory, source_model="claude")
        json_store.save(pkg)
        pkg = packager.update(pkg, StructuredMemory())
        json_store.save(pkg)

        store = get_store("sqlite", base_dir=tmp_path)
        try:
            assert isinstance(store, SQLiteStore)
            assert store.list_packages() == ["legacy_pkg"]
            assert store.list_versions("legacy_pkg") == [1, 2]
            assert store.load("legacy_pkg").version == 2
            # JSON files were left untouched
            assert (tmp_path / "legacy_pkg" / "latest.json").exists()
            # Idempotent
            assert import_legacy_json(tmp_path, store) == []
        finally:
            store.close()

    def test_factory_respects_settings_backend(self, tmp_path):
        from contextbridge.config import Settings

        s = Settings(storage_dir=tmp_path, storage_backend="json")
        assert isinstance(get_store(settings=s), JSONStore)
        with pytest.raises(ValueError):
            get_store("mongo", base_dir=tmp_path)


class TestPortablePackages:
    def test_roundtrip(self, sample_memory):
        pkg = ContextPackager().create("port", sample_memory, source_model="gpt")
        text = dumps_package(pkg)
        data = json.loads(text)
        assert data["format"] == "contextbridge-package"
        restored = import_package(text)
        assert restored.name == "port"
        assert restored.memory.total_items == sample_memory.total_items
        assert [i.id for i in restored.memory.all_items] == [i.id for i in pkg.memory.all_items]

    def test_bare_package_and_rename(self, sample_memory):
        pkg = ContextPackager().create("bare", sample_memory)
        restored = import_package(pkg.model_dump_json(), rename="renamed")
        assert restored.name == "renamed"
        assert isinstance(restored, ContextPackage)

    @pytest.mark.parametrize(
        "payload",
        ["not json", "[1, 2, 3]", json.dumps({"hello": "world"}), json.dumps({"package": {}})],
    )
    def test_rejects_bad_documents(self, payload):
        with pytest.raises(ValidationError):
            import_package(payload)

    def test_rejects_oversized(self):
        with pytest.raises(ValidationError):
            import_package("x" * 100, max_bytes=10)

    def test_rejects_bad_name(self, sample_memory):
        pkg = ContextPackager().create("ok", sample_memory)
        with pytest.raises(ValidationError):
            import_package(pkg.model_dump_json(), rename="../bad")
