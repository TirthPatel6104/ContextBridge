"""Abstract storage backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from contextbridge.models import ContextPackage, EgressRecord


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

    # -- Egress ledger -------------------------------------------------------
    #
    # Every prompt build appends a record saying which item ids were rendered
    # for which target.  The default keeps records in memory (enough for tests
    # and ad-hoc backends); SQLite and JSON persist them.

    def record_egress(self, record: EgressRecord) -> EgressRecord:
        """Persist one ledger entry and return it (with an id when the backend assigns one)."""
        ledger = self._memory_ledger()
        record.id = len(ledger) + 1
        ledger.append(record)
        return record

    def egress_records(self, name: str, *, limit: int = 200) -> list[EgressRecord]:
        """Ledger entries for a package, newest first."""
        rows = [r for r in self._memory_ledger() if r.package_name == name]
        rows.sort(key=lambda r: (r.timestamp, r.id or 0), reverse=True)
        return rows[:limit]

    def clear_egress(self, name: str) -> int:
        """Delete the ledger for a package; returns how many entries were removed."""
        ledger = self._memory_ledger()
        before = len(ledger)
        ledger[:] = [r for r in ledger if r.package_name != name]
        return before - len(ledger)

    def _memory_ledger(self) -> list[EgressRecord]:
        ledger = getattr(self, "_egress_ledger", None)
        if ledger is None:
            ledger = []
            self._egress_ledger = ledger
        return ledger

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
