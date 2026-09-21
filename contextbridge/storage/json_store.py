"""JSON file-based storage backend (legacy / portable format)."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from contextbridge.models import ContextPackage, EgressRecord
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.validation import is_valid_package_name, validate_package_name

logger = logging.getLogger(__name__)

_DEFAULT_STORAGE_DIR = Path.home() / ".contextbridge"
_EGRESS_FILE = "egress.jsonl"


class JSONStore(StorageBackend):
    """
    File-based JSON storage for context packages.

    Directory layout::

        ~/.contextbridge/
        ├── my_project/
        │   ├── context_v1.json
        │   ├── context_v2.json
        │   └── latest.json       ← copy of the current version
        └── another_project/
            └── context_v1.json

    Each version is stored as a separate file so history is preserved
    on disk and individual versions can be inspected without loading
    the full package.  This backend is kept for backward compatibility and
    because the files are easy to inspect by hand; :class:`SQLiteStore` is
    the recommended durable backend.
    """

    backend_name = "json"

    def __init__(self, base_dir: str | Path | None = None) -> None:
        env_dir = os.getenv("CB_STORAGE_DIR")
        if base_dir:
            self._base = Path(base_dir)
        elif env_dir:
            self._base = Path(env_dir)
        else:
            self._base = _DEFAULT_STORAGE_DIR
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    def location(self) -> str:
        return str(self._base)

    def _pkg_dir(self, name: str) -> Path:
        return self._base / validate_package_name(name)

    # -- StorageBackend interface -------------------------------------------

    def save(self, package: ContextPackage) -> None:
        pkg_dir = self._pkg_dir(package.name)
        pkg_dir.mkdir(parents=True, exist_ok=True)

        data = package.model_dump_json(indent=2)
        version_file = pkg_dir / f"context_v{package.version}.json"
        _atomic_write(version_file, data)
        _atomic_write(pkg_dir / "latest.json", data)

        logger.info("Saved package '%s' v%d", package.name, package.version)

    def load(self, name: str, *, version: int | None = None) -> ContextPackage:
        pkg_dir = self._pkg_dir(name)
        if not pkg_dir.exists():
            raise PackageNotFoundError(f"No package named '{name}' found")

        if version is not None:
            target = pkg_dir / f"context_v{version}.json"
            if not target.exists():
                raise PackageNotFoundError(f"Version {version} of package '{name}' not found")
        else:
            target = pkg_dir / "latest.json"
            if not target.exists():
                versions = self.list_versions(name)
                if not versions:
                    raise PackageNotFoundError(f"No versions found for '{name}'")
                target = pkg_dir / f"context_v{versions[-1]}.json"

        raw = target.read_text(encoding="utf-8")
        return ContextPackage.model_validate_json(raw)

    def list_packages(self) -> list[str]:
        if not self._base.exists():
            return []
        return sorted(
            d.name
            for d in self._base.iterdir()
            if d.is_dir() and is_valid_package_name(d.name) and (d / "latest.json").exists()
        )

    def delete(self, name: str) -> None:
        pkg_dir = self._pkg_dir(name)
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
            logger.info("Deleted package '%s'", name)
        else:
            logger.warning("Package '%s' not found for deletion", name)

    def exists(self, name: str) -> bool:
        if not is_valid_package_name(name):
            return False
        return (self._base / name.strip() / "latest.json").exists()

    def list_versions(self, name: str) -> list[int]:
        """List all stored version numbers for a package."""
        if not is_valid_package_name(name):
            return []
        pkg_dir = self._base / name.strip()
        if not pkg_dir.exists():
            return []
        versions = []
        for f in pkg_dir.glob("context_v*.json"):
            try:
                versions.append(int(f.stem.split("_v")[1]))
            except (IndexError, ValueError):
                continue
        return sorted(versions)

    # -- Egress ledger (one JSON line per event, per package) ----------------

    def record_egress(self, record: EgressRecord) -> EgressRecord:
        pkg_dir = self._pkg_dir(record.package_name)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        path = pkg_dir / _EGRESS_FILE
        existing = self._read_ledger(path)
        record.id = (existing[-1].id or 0) + 1 if existing else 1
        with path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
        return record

    def egress_records(self, name: str, *, limit: int = 200) -> list[EgressRecord]:
        if not is_valid_package_name(name):
            return []
        rows = self._read_ledger(self._base / name.strip() / _EGRESS_FILE)
        rows.sort(key=lambda r: (r.timestamp, r.id or 0), reverse=True)
        return rows[:limit]

    def clear_egress(self, name: str) -> int:
        if not is_valid_package_name(name):
            return 0
        path = self._base / name.strip() / _EGRESS_FILE
        count = len(self._read_ledger(path))
        if path.exists():
            path.unlink()
        return count

    @staticmethod
    def _read_ledger(path: Path) -> list[EgressRecord]:
        if not path.exists():
            return []
        rows: list[EgressRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(EgressRecord.model_validate_json(line))
            except ValueError:
                continue  # a torn line from a crash mid-write: skip it
        return rows


def _atomic_write(path: Path, data: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written package."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)
