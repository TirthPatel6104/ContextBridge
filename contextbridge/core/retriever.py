"""
Memory Retriever — semantic relevance filtering via embeddings.

Implements Retrieval-Augmented Memory: given a user query, embeds it,
searches the vector store for the most relevant memory items, and
returns only the context that matters.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from contextbridge.models import MemoryItem, StructuredMemory

if TYPE_CHECKING:
    from contextbridge.core.llm_interface import LLMInterface
    from contextbridge.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    Retrieval-Augmented Memory — injects only relevant context.

    Usage::

        retriever = MemoryRetriever(vector_store)
        await retriever.index(memory, adapter)
        relevant = await retriever.query("system design", adapter, top_k=5)
    """

    def __init__(self, vector_store: VectorStore) -> None:
        self._store = vector_store
        self._indexed_items: list[MemoryItem] = []

    @property
    def is_indexed(self) -> bool:
        """Whether memory has been indexed."""
        return len(self._indexed_items) > 0

    async def index(
        self,
        memory: StructuredMemory,
        adapter: LLMInterface,
    ) -> int:
        """
        Embed and index all memory items for later retrieval.

        Parameters
        ----------
        memory : StructuredMemory
            The memory to index.
        adapter : LLMInterface
            The adapter to use for generating embeddings.

        Returns
        -------
        int
            The number of items indexed.
        """
        items = memory.all_items
        if not items:
            logger.warning("No memory items to index")
            return 0

        texts = [item.content for item in items]
        logger.info("Embedding %d memory items via %s", len(texts), adapter.name)

        embeddings = await adapter.embed_batch(texts)

        # Store embeddings on items for potential later use
        for item, emb in zip(items, embeddings):
            item.embedding = emb

        self._store.add(texts, embeddings)
        self._indexed_items = list(items)

        logger.info("Indexed %d items into vector store", len(items))
        return len(items)

    async def query(
        self,
        user_query: str,
        adapter: LLMInterface,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[MemoryItem]:
        """
        Retrieve the most relevant memory items for a user query.

        Parameters
        ----------
        user_query : str
            The user's question or prompt.
        adapter : LLMInterface
            The adapter to use for embedding the query.
        top_k : int, optional
            Maximum number of results to return.
        min_score : float, optional
            Minimum similarity score threshold (0-1).

        Returns
        -------
        list[MemoryItem]
            The most relevant memory items, sorted by relevance.
        """
        if not self.is_indexed:
            logger.warning("Memory not indexed — returning empty results")
            return []

        query_embedding = await adapter.embed(user_query)
        results = self._store.search(query_embedding, top_k=top_k)

        relevant: list[MemoryItem] = []
        for text, score in results:
            if score < min_score:
                continue
            # Find the corresponding MemoryItem
            for item in self._indexed_items:
                if item.content == text:
                    relevant.append(item)
                    break

        logger.info(
            "Query '%s' → %d relevant items (top_k=%d, min_score=%.2f)",
            user_query[:50],
            len(relevant),
            top_k,
            min_score,
        )
        return relevant

    async def query_and_filter(
        self,
        user_query: str,
        memory: StructuredMemory,
        adapter: LLMInterface,
        *,
        top_k: int = 5,
    ) -> StructuredMemory:
        """
        Query and return a filtered StructuredMemory containing only
        relevant items.  Useful for building targeted prompts.
        """
        relevant_items = await self.query(user_query, adapter, top_k=top_k)
        relevant_contents = {item.content for item in relevant_items}

        filtered = StructuredMemory()
        from contextbridge.models import MemoryCategory

        for cat in MemoryCategory:
            items = [item for item in memory.get_category(cat) if item.content in relevant_contents]
            setattr(filtered, cat.value, items)

        return filtered

    def clear(self) -> None:
        """Clear the index."""
        self._store.clear()
        self._indexed_items.clear()
        logger.info("Cleared retriever index")
