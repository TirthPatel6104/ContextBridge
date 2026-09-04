"""Tests for the Memory Retriever and Vector Store."""

import pytest

from contextbridge.core.retriever import MemoryRetriever
from contextbridge.storage.vector_store import VectorStore


class TestVectorStore:
    def test_add_and_search(self):
        store = VectorStore(dimension=4)
        texts = ["hello world", "goodbye world", "python rocks"]
        embeddings = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ]
        store.add(texts, embeddings)
        assert store.size == 3

        results = store.search([1.0, 0.0, 0.0, 0.0], top_k=2)
        assert len(results) == 2
        assert results[0][0] == "hello world"

    def test_empty_search(self):
        store = VectorStore(dimension=4)
        results = store.search([1.0, 0.0, 0.0, 0.0])
        assert results == []

    def test_clear(self):
        store = VectorStore(dimension=4)
        store.add(["test"], [[1.0, 0.0, 0.0, 0.0]])
        assert store.size == 1
        store.clear()
        assert store.size == 0

    def test_save_load(self, tmp_path):
        store = VectorStore(dimension=4)
        store.add(["hello", "world"], [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])

        path = tmp_path / "test_index.pkl"
        store.save(path)

        loaded = VectorStore.load(path)
        assert loaded.size == 2
        results = loaded.search([1.0, 0.0, 0.0, 0.0], top_k=1)
        assert results[0][0] == "hello"

    def test_mismatched_lengths(self):
        store = VectorStore(dimension=4)
        with pytest.raises(ValueError):
            store.add(["one", "two"], [[1.0, 0.0, 0.0, 0.0]])


class TestMemoryRetriever:
    @pytest.mark.asyncio
    async def test_index_and_query(self, mock_adapter, sample_memory):
        store = VectorStore(dimension=256)
        retriever = MemoryRetriever(store)

        count = await retriever.index(sample_memory, mock_adapter)
        assert count == sample_memory.total_items
        assert retriever.is_indexed

        results = await retriever.query("Python project", mock_adapter, top_k=3)
        assert len(results) > 0
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_query_empty_index(self, mock_adapter):
        store = VectorStore(dimension=256)
        retriever = MemoryRetriever(store)

        results = await retriever.query("anything", mock_adapter)
        assert results == []

    @pytest.mark.asyncio
    async def test_query_and_filter(self, mock_adapter, sample_memory):
        store = VectorStore(dimension=256)
        retriever = MemoryRetriever(store)
        await retriever.index(sample_memory, mock_adapter)

        filtered = await retriever.query_and_filter(
            "architecture decisions", sample_memory, mock_adapter, top_k=3
        )
        assert filtered.total_items <= sample_memory.total_items

    @pytest.mark.asyncio
    async def test_clear(self, mock_adapter, sample_memory):
        store = VectorStore(dimension=256)
        retriever = MemoryRetriever(store)
        await retriever.index(sample_memory, mock_adapter)
        assert retriever.is_indexed

        retriever.clear()
        assert not retriever.is_indexed
