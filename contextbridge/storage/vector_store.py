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
    queries.  The index can be serialised to / from disk.

    Usage::

        store = VectorStore(dimension=1536)
        store.add(["fact 1", "fact 2"], [emb1, emb2])
        results = store.search(query_emb, top_k=3)
        store.save("index.pkl")
    """

    def __init__(self, dimension: int = 1536) -> None:
        self._dimension = dimension
        self._texts: list[str] = []
        self._index = None  # lazy import
        self._initialised = False

    def _ensure_index(self) -> None:
        """Lazily initialise the FAISS index."""
        if self._initialised:
            return
        try:
            import faiss

            self._index = faiss.IndexFlatIP(self._dimension)  # inner product
            self._initialised = True
        except ImportError:
            # Fallback to pure numpy if FAISS is not installed
            logger.warning(
                "FAISS not installed — falling back to numpy brute-force search. "
                "Install faiss-cpu for better performance: pip install faiss-cpu"
            )
            self._index = None
            self._initialised = True
            self._embeddings: list[np.ndarray] = []

    def add(self, texts: list[str], embeddings: list[list[float]]) -> None:
        """
        Add texts and their embeddings to the store.

        Parameters
        ----------
        texts : list[str]
            The text strings to store.
        embeddings : list[list[float]]
            Corresponding embedding vectors.
        """
        if len(texts) != len(embeddings):
            raise ValueError("texts and embeddings must have the same length")

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

        Parameters
        ----------
        query_embedding : list[float]
            The query embedding vector.
        top_k : int, optional
            Number of results to return.

        Returns
        -------
        list[tuple[str, float]]
            List of (text, similarity_score) tuples, sorted by relevance.
        """
        if not self._texts:
            return []

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
        else:
            # Numpy fallback
            matrix = np.array(self._embeddings, dtype=np.float32)
            similarities = matrix @ query.T
            similarities = similarities.flatten()
            top_indices = np.argsort(similarities)[::-1][:top_k]
            return [(self._texts[i], float(similarities[i])) for i in top_indices]

    def clear(self) -> None:
        """Clear the store."""
        self._texts.clear()
        self._initialised = False
        self._index = None
        if hasattr(self, "_embeddings"):
            self._embeddings.clear()

    @property
    def size(self) -> int:
        """Number of items in the store."""
        return len(self._texts)

    # -- Persistence --------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Serialise the store to disk."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "dimension": self._dimension,
            "texts": self._texts,
        }

        if self._index is not None:
            import tempfile

            import faiss

            with tempfile.NamedTemporaryFile(delete=False, suffix=".faiss") as tmp:
                faiss.write_index(self._index, tmp.name)
                with open(tmp.name, "rb") as f:
                    state["faiss_index"] = f.read()
        elif hasattr(self, "_embeddings") and self._embeddings:
            state["embeddings"] = [e.tolist() for e in self._embeddings]

        with open(path, "wb") as f:
            pickle.dump(state, f)
        logger.info("Saved vector store (%d items) → %s", self.size, path)

    @classmethod
    def load(cls, path: str | Path) -> VectorStore:
        """Load a vector store from disk."""
        path = Path(path)
        with open(path, "rb") as f:
            state = pickle.load(f)

        store = cls(dimension=state["dimension"])
        store._texts = state["texts"]

        if "faiss_index" in state:
            import tempfile

            import faiss

            with tempfile.NamedTemporaryFile(delete=False, suffix=".faiss") as tmp:
                tmp.write(state["faiss_index"])
                tmp.flush()
                store._index = faiss.read_index(tmp.name)
                store._initialised = True
        elif "embeddings" in state:
            store._embeddings = [np.array(e) for e in state["embeddings"]]
            store._initialised = True

        logger.info("Loaded vector store (%d items) ← %s", store.size, path)
        return store
