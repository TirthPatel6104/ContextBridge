"""Vector store — FAISS-backed similarity search for memory retrieval."""

from __future__ import annotations

import logging
import pickle
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class VectorStore:
    """
    In-memory vector store backed by FAISS for fast similarity search.

    Stores text → embedding mappings and supports top-k nearest-neighbour
    queries.  The dimension is inferred from the first batch of vectors when
    not given explicitly, so callers do not need to know which embedding
    model an adapter uses.

    Usage::

        store = VectorStore()
        store.add(["fact 1", "fact 2"], [emb1, emb2])
        results = store.search(query_emb, top_k=3)
    """

    def __init__(self, dimension: int | None = None) -> None:
        self._dimension = dimension
        self._texts: list[str] = []
        self._index = None  # lazy import
        self._initialised = False
        self._embeddings: list[np.ndarray] = []

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def _ensure_index(self) -> None:
        """Lazily initialise the FAISS index (or the numpy fallback)."""
        if self._initialised:
            return
        if self._dimension is None:
            raise ValueError("Vector dimension unknown — add vectors before searching")
        try:
            import faiss

            self._index = faiss.IndexFlatIP(self._dimension)  # inner product
        except ImportError:
            logger.warning(
                "FAISS not installed — falling back to numpy brute-force search. "
                "Install faiss-cpu for better performance: pip install faiss-cpu"
            )
            self._index = None
        self._initialised = True

    def add(self, texts: list[str], embeddings: list[list[float]]) -> None:
        """
        Add texts and their embeddings to the store.

        Raises
        ------
        ValueError
            If lengths differ, any embedding is empty, or dimensions mismatch.
        """
        if len(texts) != len(embeddings):
            raise ValueError("texts and embeddings must have the same length")
        if not texts:
            return
        lengths = {len(e) for e in embeddings}
        if len(lengths) != 1 or 0 in lengths:
            raise ValueError("All embeddings must be non-empty and share one dimension")
        dim = lengths.pop()
        if self._dimension is None:
            self._dimension = dim
        elif dim != self._dimension:
            raise ValueError(
                f"Embedding dimension {dim} does not match store dimension {self._dimension}"
            )

        self._ensure_index()
        vectors = np.array(embeddings, dtype=np.float32)

        # Normalise for cosine similarity via inner product
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1
        vectors = vectors / norms

        if self._index is not None:
            self._index.add(vectors)
        else:
            self._embeddings.extend(vectors)

        self._texts.extend(texts)
        logger.debug("Added %d vectors (total: %d)", len(texts), len(self._texts))

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """
        Search for the most similar texts to the query.

        Returns
        -------
        list[tuple[str, float]]
            ``(text, cosine_similarity)`` tuples sorted by relevance.
        """
        if not self._texts:
            return []
        if len(query_embedding) != self._dimension:
            raise ValueError(
                f"Query dimension {len(query_embedding)} does not match store dimension "
                f"{self._dimension}"
            )

        self._ensure_index()
        query = np.array([query_embedding], dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm > 0:
            query = query / norm

        top_k = min(top_k, len(self._texts))

        if self._index is not None:
            scores, indices = self._index.search(query, top_k)
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:
                    continue
                results.append((self._texts[idx], float(score)))
            return results

        matrix = np.array(self._embeddings, dtype=np.float32)
        similarities = (matrix @ query.T).flatten()
        top_indices = np.argsort(similarities)[::-1][:top_k]
        return [(self._texts[i], float(similarities[i])) for i in top_indices]

    def clear(self) -> None:
        """Clear the store."""
        self._texts.clear()
        self._embeddings.clear()
        self._initialised = False
        self._index = None

    @property
    def size(self) -> int:
        """Number of items in the store."""
        return len(self._texts)

    # -- Persistence --------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Serialise the store to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        state: dict = {"dimension": self._dimension, "texts": self._texts}

        if self._index is not None:
            import faiss

            state["faiss_index"] = faiss.serialize_index(self._index).tobytes()
        elif self._embeddings:
            state["embeddings"] = [e.tolist() for e in self._embeddings]

        with open(path, "wb") as f:
            pickle.dump(state, f)
        logger.info("Saved vector store (%d items)", self.size)

    @classmethod
    def load(cls, path: str | Path) -> VectorStore:
        """Load a vector store previously written by :meth:`save`.

        Only load files you created yourself: the format is a pickle.
        """
        path = Path(path)
        with open(path, "rb") as f:
            state = pickle.load(f)  # noqa: S301 - local trusted file

        store = cls(dimension=state["dimension"])
        store._texts = list(state["texts"])

        if "faiss_index" in state:
            import faiss

            store._index = faiss.deserialize_index(
                np.frombuffer(state["faiss_index"], dtype=np.uint8)
            )
            store._initialised = True
        elif "embeddings" in state:
            store._embeddings = [np.array(e, dtype=np.float32) for e in state["embeddings"]]
            store._initialised = True

        logger.info("Loaded vector store (%d items)", store.size)
        return store
