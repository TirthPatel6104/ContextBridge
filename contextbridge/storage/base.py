"""Abstract storage backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from contextbridge.models import ContextPackage


class StorageBackend(ABC):
    """
    Abstract interface for persistent context package storage.

    Implementations handle serialisation, file I/O, and versioned retrieval.
    """

    @abstractmethod
    def save(self, package: ContextPackage) -> None:
        """Persist a context package to storage."""
        ...

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
        """
        ...

    @abstractmethod
    def list_packages(self) -> list[str]:
        """List all stored package names."""
        ...

    @abstractmethod
    def delete(self, name: str) -> None:
        """Delete a stored package and all its versions."""
        ...

    @abstractmethod
    def exists(self, name: str) -> bool:
        """Check whether a package exists in storage."""
        ...
