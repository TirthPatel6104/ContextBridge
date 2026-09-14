"""Tests for the explainable retriever, lexical scorer, and vector store."""

from __future__ import annotations

import pytest

from contextbridge.core.retriever import LexicalScorer, MemoryRetriever, tokenize
from contextbridge.models import (
    MemoryCategory,
    MemoryItem,
    RetrievalOptions,
    StructuredMemory,
)
from contextbridge.storage.vector_store import VectorStore
from tests.conftest import MockAdapter


class TestVectorStore:
    def test_add_and_search_infers_dimension(self):
        store = VectorStore()
        store.add(
            ["hello world", "goodbye world", "python rocks"],
            [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]],
        )
        assert store.size == 3
        assert store.dimension == 4
        results = store.search([1.0, 0.0, 0.0, 0.0], top_k=2)
        assert len(results) == 2
        assert results[0][0] == "hello world"

    def test_empty_search(self):
        assert VectorStore(dimension=4).search([1.0, 0.0, 0.0, 0.0]) == []

    def test_dimension_mismatch(self):
        store = VectorStore()
        store.add(["a"], [[1.0, 0.0]])
        with pytest.raises(ValueError):
            store.add(["b"], [[1.0, 0.0, 0.0]])
        with pytest.raises(ValueError):
            store.search([1.0, 0.0, 0.0])

    def test_rejects_empty_or_ragged(self):
        store = VectorStore()
        with pytest.raises(ValueError):
            store.add(["a"], [[]])
        with pytest.raises(ValueError):
            store.add(["a", "b"], [[1.0, 0.0], [1.0]])
        with pytest.raises(ValueError):
            store.add(["one", "two"], [[1.0, 0.0, 0.0, 0.0]])

    def test_clear(self):
        store = VectorStore()
        store.add(["test"], [[1.0, 0.0, 0.0, 0.0]])
        store.clear()
        assert store.size == 0

    def test_save_load(self, tmp_path):
        store = VectorStore()
        store.add(["hello", "world"], [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
        path = tmp_path / "test_index.pkl"
        store.save(path)
        loaded = VectorStore.load(path)
        assert loaded.size == 2
        assert loaded.search([1.0, 0.0, 0.0, 0.0], top_k=1)[0][0] == "hello"


class TestTokenizer:
    def test_drops_stopwords_and_stems(self):
        assert tokenize("The decisions about the databases") == ["decision", "database"]

    def test_keeps_technical_tokens(self):
        toks = tokenize("Use PostgreSQL 15 and SQLAlchemy 2.0")
        assert "postgresql" in toks and "sqlalchemy" in toks and "15" in toks


class TestLexicalScorer:
    def test_scores_are_bounded_and_explainable(self, sample_memory):
        scorer = LexicalScorer(sample_memory.all_items)
        scores = scorer.score("FAISS vector search")
        assert all(0.0 <= s <= 1.0 for s, _ in scores)
        best = max(scores, key=lambda x: x[0])
        assert set(best[1]) == {"faiss", "vector", "search"}

    def test_no_query_terms(self, sample_memory):
        scorer = LexicalScorer(sample_memory.all_items)
        assert all(s == 0.0 for s, _ in scorer.score("the and of"))


class TestRetrieverLexical:
    def test_ranks_by_relevance_with_evidence(self, sample_memory):
        r = MemoryRetriever()
        assert r.index_sync(sample_memory) == sample_memory.total_items
        result = r.retrieve_sync("FAISS vector search", RetrievalOptions(top_k=3))
        assert result.method == "lexical"
        assert result.selected[0].item.content == "Using FAISS for vector search"
        assert result.selected[0].rank == 1
        assert "faiss" in result.selected[0].matched_terms
        assert "Matched" in result.selected[0].reason
        # Items without any evidence are not selected
        assert all(s.score > 0 for s in result.selected)
        assert result.total_items == sample_memory.total_items

    def test_is_deterministic(self, sample_memory):
        r = MemoryRetriever()
        r.index_sync(sample_memory)
        a = r.retrieve_sync("python project", RetrievalOptions(top_k=5))
        b = r.retrieve_sync("python project", RetrievalOptions(top_k=5))
        assert [s.item.id for s in a.selected] == [s.item.id for s in b.selected]
        assert [s.score for s in a.selected] == [s.score for s in b.selected]

    def test_category_filter(self, sample_memory):
        r = MemoryRetriever()
        r.index_sync(sample_memory)
        result = r.retrieve_sync(
            "Pydantic FAISS", RetrievalOptions(categories=[MemoryCategory.OPEN_TASKS])
        )
        assert result.considered == 1
        assert result.selected == []  # the task does not mention either term
        result = r.retrieve_sync(
            "Pydantic FAISS", RetrievalOptions(categories=[MemoryCategory.DECISIONS])
        )
        assert {s.item.category for s in result.selected} == {MemoryCategory.DECISIONS}

    def test_top_k_and_budget(self, sample_memory):
        r = MemoryRetriever()
        r.index_sync(sample_memory)
        result = r.retrieve_sync("Pydantic FAISS data models", RetrievalOptions(top_k=1))
        assert len(result.selected) == 1
        tight = r.retrieve_sync(
            "Pydantic FAISS data models", RetrievalOptions(top_k=5, token_budget=8)
        )
        assert tight.tokens_selected <= 8
        assert tight.dropped_for_budget >= 1

    def test_min_score(self, sample_memory):
        r = MemoryRetriever()
        r.index_sync(sample_memory)
        result = r.retrieve_sync("Pydantic", RetrievalOptions(min_score=0.99))
        assert result.selected == []
        assert result.dropped_below_min_score >= 1

    def test_empty_query_ranks_by_confidence(self, sample_memory):
        r = MemoryRetriever()
        r.index_sync(sample_memory)
        result = r.retrieve_sync("", RetrievalOptions(top_k=10))
        assert len(result.selected) == sample_memory.total_items
        confidences = [s.item.confidence for s in result.selected]
        assert confidences == sorted(confidences, reverse=True)
        assert "confidence" in result.selected[0].reason

    def test_token_accounting(self, sample_memory):
        r = MemoryRetriever()
        r.index_sync(sample_memory)
        result = r.retrieve_sync("FAISS", RetrievalOptions(top_k=1), transcript_tokens=1000)
        assert result.tokens_selected > 0
        assert result.tokens_full_memory > result.tokens_selected
        assert result.tokens_saved_vs_full_memory == (
            result.tokens_full_memory - result.tokens_selected
        )
        assert result.tokens_saved_vs_transcript == 1000 - result.tokens_selected
        summary = result.summary()
        assert summary["selected"] == 1 and summary["method"] == "lexical"

    def test_empty_memory(self):
        r = MemoryRetriever()
        r.index_sync(StructuredMemory())
        result = r.retrieve_sync("anything")
        assert result.selected == [] and result.total_items == 0


class TestRetrieverEmbeddings:
    @pytest.mark.asyncio
    async def test_hybrid_when_adapter_is_semantic(self, sample_memory):
        adapter = MockAdapter(semantic=True)
        r = MemoryRetriever(VectorStore())
        await r.index(sample_memory, adapter)
        assert r.uses_embeddings
        result = await r.retrieve("FAISS vector search", adapter, RetrievalOptions(top_k=3))
        assert result.method == "hybrid"
        assert result.selected[0].embedding_score is not None
        assert "embedding similarity" in result.selected[0].reason

    @pytest.mark.asyncio
    async def test_non_semantic_adapter_stays_lexical(self, sample_memory):
        adapter = MockAdapter(semantic=False)
        r = MemoryRetriever(VectorStore())
        await r.index(sample_memory, adapter)
        assert not r.uses_embeddings
        assert adapter.embed_calls == 0
        result = await r.retrieve("FAISS", adapter)
        assert result.method == "lexical"

    @pytest.mark.asyncio
    async def test_embedding_failure_falls_back(self, sample_memory):
        adapter = MockAdapter(fail_embeddings=True)
        r = MemoryRetriever(VectorStore())
        await r.index(sample_memory, adapter)
        assert not r.uses_embeddings
        result = await r.retrieve("FAISS", adapter)
        assert result.method == "lexical"
        assert result.selected

    @pytest.mark.asyncio
    async def test_legacy_query_helpers(self, mock_adapter, sample_memory):
        r = MemoryRetriever(VectorStore())
        await r.index(sample_memory, mock_adapter)
        items = await r.query("FAISS vector search", mock_adapter, top_k=2)
        assert 0 < len(items) <= 2
        assert all(isinstance(i, MemoryItem) for i in items)
        filtered = await r.query_and_filter("FAISS", sample_memory, mock_adapter, top_k=2)
        assert filtered.total_items <= 2

    @pytest.mark.asyncio
    async def test_query_empty_index(self, mock_adapter):
        r = MemoryRetriever(VectorStore())
        assert await r.query("anything", mock_adapter) == []

    @pytest.mark.asyncio
    async def test_clear(self, mock_adapter, sample_memory):
        r = MemoryRetriever(VectorStore())
        await r.index(sample_memory, mock_adapter)
        r.clear()
        assert not r.is_indexed
