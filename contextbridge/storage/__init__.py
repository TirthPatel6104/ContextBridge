"""Storage package — factory for storage backends."""

from __future__ import annotations

from contextbridge.storage.base import StorageBackend
from contextbridge.storage.json_store import JSONStore
from contextbridge.storage.vector_store import VectorStore


def get_store(backend: str = "json", **kwargs) -> StorageBackend:
    """
    Factory function to instantiate a storage backend.

    Parameters
    ----------
    backend : str
        Backend name: 'json' (default).
    **kwargs
        Passed to the backend constructor.
    """
    backend = backend.lower().strip()
    if backend == "json":
        return JSONStore(**kwargs)
    raise ValueError(f"Unknown storage backend '{backend}'. Choose from: json")


__all__ = ["get_store", "JSONStore", "VectorStore", "StorageBackend"]
