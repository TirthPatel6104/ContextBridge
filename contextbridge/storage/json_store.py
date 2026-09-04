"""JSON file-based storage backend."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from contextbridge.models import ContextPackage
from contextbridge.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_DEFAULT_STORAGE_DIR = Path.home() / ".contextbridge"


class JSONStore(StorageBackend):
    """
    File-based JSON storage for context packages.

    Directory layout::

        ~/.contextbridge/
        ├── my_project/
        │   ├── context_v1.json
        │   ├── context_v2.json
        │   └── latest.json       ← symlink / copy of highest version
        └── another_project/
            └── context_v1.json

    Each version is stored as a separate file so history is preserved
    on disk and individual versions can be inspected without loading
    the full package.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        import os

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

    # -- StorageBackend interface -------------------------------------------

    def save(self, package: ContextPackage) -> None:
        pkg_dir = self._base / package.name
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Save versioned file
        version_file = pkg_dir / f"context_v{package.version}.json"
        data = package.model_dump_json(indent=2)
        version_file.write_text(data, encoding="utf-8")

        # Save a "latest" copy for quick access
        latest_file = pkg_dir / "latest.json"
        latest_file.write_text(data, encoding="utf-8")

        logger.info(
            "Saved package '%s' v%d → %s",
            package.name,
            package.version,
            version_file,
        )

    def load(self, name: str, *, version: int | None = None) -> ContextPackage:
        pkg_dir = self._base / name
        if not pkg_dir.exists():
            raise FileNotFoundError(f"No package named '{name}' found")

        if version is not None:
            target = pkg_dir / f"context_v{version}.json"
            if not target.exists():
                raise FileNotFoundError(f"Version {version} of package '{name}' not found")
        else:
            target = pkg_dir / "latest.json"
            if not target.exists():
                # Fallback: find highest version
                versions = sorted(pkg_dir.glob("context_v*.json"))
                if not versions:
                    raise FileNotFoundError(f"No versions found for '{name}'")
                target = versions[-1]

        raw = target.read_text(encoding="utf-8")
        package = ContextPackage.model_validate_json(raw)
        logger.info("Loaded package '%s' v%d", package.name, package.version)
        return package

    def list_packages(self) -> list[str]:
        if not self._base.exists():
            return []
        return sorted(
            d.name for d in self._base.iterdir() if d.is_dir() and (d / "latest.json").exists()
        )

    def delete(self, name: str) -> None:
        pkg_dir = self._base / name
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
            logger.info("Deleted package '%s'", name)
        else:
            logger.warning("Package '%s' not found for deletion", name)

    def exists(self, name: str) -> bool:
        return (self._base / name / "latest.json").exists()

    # -- Convenience --------------------------------------------------------

    def list_versions(self, name: str) -> list[int]:
        """List all stored version numbers for a package."""
        pkg_dir = self._base / name
        if not pkg_dir.exists():
            return []
        versions = []
        for f in pkg_dir.glob("context_v*.json"):
            try:
                v = int(f.stem.split("_v")[1])
                versions.append(v)
            except (IndexError, ValueError):
                continue
        return sorted(versions)
