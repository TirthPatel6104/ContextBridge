"""Abstract storage backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from contextbridge.models import ContextPackage


class PackageNotFoundError(FileNotFoundError):
    """Raised when a package (or a specific version of it) does not exist."""


class StorageBackend(ABC):
    """
    Abstract interface for persistent context package storage.

    Implementations handle serialisation, I/O, and versioned retrieval.
    All implementations must validate package names with
    :func:`contextbridge.validation.validate_package_name` before touching
    the filesystem or database.
    """

    #: Short identifier shown in the CLI / dashboard ("json", "sqlite").
    backend_name: str = "abstract"

    @abstractmethod
    def save(self, package: ContextPackage) -> None:
        """Persist a context package (the given version becomes the latest)."""

    @abstractmethod
    def load(self, name: str, *, version: int | None = None) -> ContextPackage:
        """
        Load a context package by name.

        Parameters
        ----------
        name : str
            The package name.
        version : int, optional
            Specific version to load.  If None, loads the latest.

        Raises
        ------
        PackageNotFoundError
            If the package or version does not exist.
        """

    @abstractmethod
    def list_packages(self) -> list[str]:
        """List all stored package names, sorted."""

    @abstractmethod
    def delete(self, name: str) -> None:
        """Delete a stored package and all its versions."""

    @abstractmethod
    def exists(self, name: str) -> bool:
        """Check whether a package exists in storage."""

    @abstractmethod
    def list_versions(self, name: str) -> list[int]:
        """List all stored version numbers for a package (empty if unknown)."""

    # -- Optional / default implementations ---------------------------------

    def summaries(self) -> list[dict[str, Any]]:
        """Lightweight listing used by the CLI and dashboard.

        The default implementation loads every package; backends that keep a
        summary table should override this.
        """
        result = []
        for name in self.list_packages():
            pkg = self.load(name)
            result.append(summary_from_package(pkg))
        return result

    def location(self) -> str:
        """Human-readable description of where data lives."""
        return self.backend_name

    def close(self) -> None:  # pragma: no cover - trivial default
        """Release any resources (connections, handles)."""
        return None

    def __enter__(self) -> StorageBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def summary_from_package(pkg: ContextPackage) -> dict[str, Any]:
    return {
        "name": pkg.name,
        "version": pkg.version,
        "source_model": pkg.source_model or "unknown",
        "total_items": pkg.memory.total_items,
        "created_at": pkg.created_at.isoformat(),
        "updated_at": pkg.updated_at.isoformat(),
        "schema_version": pkg.schema_version,
    }
