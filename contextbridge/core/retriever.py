"""
Memory Retriever — explainable relevance filtering over memory items.

Given a user query, the retriever scores every memory item and returns a
:class:`~contextbridge.models.RetrievalResult` that says *which* items were
selected, *how* they scored, *which query terms* matched, and how many
estimated tokens the selection costs compared with sending everything.

Two scorers are combined:

* **Lexical (always on).**  A BM25-style scorer over item content and source
  excerpts.  It is deterministic, needs no network, and produces the matched
  terms that make a selection explainable.
* **Embedding (optional).**  When an adapter provides *semantic* embeddings
  (e.g. OpenAI ``text-embedding-3-small``) cosine similarity is blended in.
  Adapters that only offer hash-based pseudo-embeddings are ignored so that
  noise never outranks real matches.
"""

from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING

from contextbridge.core.tokens import estimate_tokens
from contextbridge.models import (
    MemoryCategory,
    MemoryItem,
    RetrievalOptions,
    RetrievalResult,
    ScoredItem,
    StructuredMemory,
)

if TYPE_CHECKING:
    from contextbridge.core.llm_interface import LLMInterface
    from contextbridge.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then else of to in on at by for with from as is are was
    were be been being am do does did done have has had having i me my mine we our
    ours you your yours he him his she her hers it its they them their theirs this
    that these those there here what which who whom whose when where why how all any
    both each few more most other some such no nor not only own same so than too very
    can could should would will shall may might must about into over under again
    further once also just now up down out off
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#._\-]*")


def _stem(token: str) -> str:
    """Very small suffix stripper — enough to match 'decisions' with 'decision'.

    Deliberately conservative: it never touches short tokens or words ending
    in "ss"/"us"/"is" (faiss, status, analysis), because a wrong stem hides
    real matches while a missed stem only costs a little recall.
    """
    if token.isdigit():
        return token
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 6 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 5 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lower-case, split, drop stop words, and lightly stem *text*."""
    tokens = []
    for raw in _TOKEN_RE.findall(text.lower()):
        raw = raw.strip("._-")
        if len(raw) < 2 or raw in STOPWORDS:
            continue
        tokens.append(_stem(raw))
    return tokens


# ---------------------------------------------------------------------------
# Lexical scorer
# ---------------------------------------------------------------------------


class LexicalScorer:
    """BM25 scorer over memory items, normalised to ``[0, 1]``.

    The normaliser is the score a document would get if every query term were
    saturated, so ``1.0`` means "every query term is strongly present" and
    ``0.0`` means "no query term appears".  Scores are therefore comparable
    across queries, unlike max-normalised scores.
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, items: list[MemoryItem]) -> None:
        self._items = items
        self._docs: list[dict[str, int]] = []
        self._lengths: list[int] = []
        df: dict[str, int] = {}
        for item in items:
            tokens = tokenize(f"{item.content} {item.source}")
            counts: dict[str, int] = {}
            for t in tokens:
                counts[t] = counts.get(t, 0) + 1
            self._docs.append(counts)
            self._lengths.append(len(tokens))
            for t in counts:
                df[t] = df.get(t, 0) + 1
        n = len(items)
        self._avgdl = (sum(self._lengths) / n) if n else 0.0
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self._n = n

    def score(self, query: str) -> list[tuple[float, list[str]]]:
        """Return ``(score, matched_terms)`` for every indexed item, in index order."""
        q_terms = list(dict.fromkeys(tokenize(query)))
        if not q_terms or not self._n:
            return [(0.0, []) for _ in self._items]

        # Unseen terms get the idf of a term appearing in no document.
        default_idf = math.log(1 + (self._n + 0.5) / 0.5)
        idfs = {t: self._idf.get(t, default_idf) for t in q_terms}
        ceiling = sum(idf * (self.K1 + 1) for idf in idfs.values()) or 1.0

        results: list[tuple[float, list[str]]] = []
        for counts, length in zip(self._docs, self._lengths):
            raw = 0.0
            matched: list[str] = []
            norm = 1 - self.B + self.B * (length / self._avgdl if self._avgdl else 1.0)
            for term in q_terms:
                tf = counts.get(term)
                if not tf:
                    continue
                matched.append(term)
                raw += idfs[term] * (tf * (self.K1 + 1)) / (tf + self.K1 * norm)
            results.append((min(1.0, raw / ceiling), matched))
        return results


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------

_CATEGORY_ORDER = {cat: i for i, cat in enumerate(MemoryCategory)}


class MemoryRetriever:
    """
    Explainable retrieval over a :class:`StructuredMemory`.

    Usage::

        retriever = MemoryRetriever()
        await retriever.index(memory)                 # lexical only
        await retriever.index(memory, adapter)        # + embeddings if semantic
        result = await retriever.retrieve("database schema", options=RetrievalOptions(top_k=5))
        for scored in result.selected:
            print(scored.rank, scored.score, scored.matched_terms, scored.item.content)
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        *,
        embedding_weight: float = 0.5,
    ) -> None:
        if not 0.0 <= embedding_weight <= 1.0:
            raise ValueError("embedding_weight must be between 0 and 1")
        self._store = vector_store
        self._embedding_weight = embedding_weight
        self._items: list[MemoryItem] = []
        self._lexical: LexicalScorer | None = None
        self._embeddings_ready = False

    # -- State ---------------------------------------------------------------

    @property
    def is_indexed(self) -> bool:
        return bool(self._items)

    @property
    def uses_embeddings(self) -> bool:
        return self._embeddings_ready

    def clear(self) -> None:
        if self._store is not None:
            self._store.clear()
        self._items = []
        self._lexical = None
        self._embeddings_ready = False

    # -- Indexing ------------------------------------------------------------

    def index_sync(self, memory: StructuredMemory) -> int:
        """Build the lexical index only (no adapter needed)."""
        self._items = list(memory.all_items)
        self._lexical = LexicalScorer(self._items)
        self._embeddings_ready = False
        if self._store is not None:
            self._store.clear()
        return len(self._items)

    async def index(
        self,
        memory: StructuredMemory,
        adapter: LLMInterface | None = None,
    ) -> int:
        """
        Index all memory items.  Embeddings are added only when *adapter*
        advertises semantic embeddings and a vector store is configured;
        any embedding failure degrades to lexical-only retrieval.
        """
        count = self.index_sync(memory)
        if count == 0:
            logger.warning("No memory items to index")
            return 0
        if adapter is None or self._store is None:
            return count
        if not getattr(adapter, "semantic_embeddings", True):
            logger.info(
                "Adapter '%s' has no semantic embeddings — lexical retrieval only", adapter.name
            )
            return count
        try:
            embeddings = await adapter.embed_batch([item.content for item in self._items])
            if len(embeddings) != count or any(not e for e in embeddings):
                raise ValueError("adapter returned incomplete embeddings")
            self._store.add([item.id for item in self._items], embeddings)
            for item, emb in zip(self._items, embeddings):
                item.embedding = emb
            self._embeddings_ready = True
            logger.info("Indexed %d items with embeddings via %s", count, adapter.name)
        except Exception as exc:  # network / dimension / provider errors
            self._embeddings_ready = False
            if self._store is not None:
                self._store.clear()
            logger.warning(
                "Embedding indexing failed (%s) — falling back to lexical retrieval",
                exc.__class__.__name__,
            )
        return count

    # -- Retrieval -----------------------------------------------------------

    def retrieve_sync(
        self,
        query: str,
        options: RetrievalOptions | None = None,
        *,
        transcript_tokens: int | None = None,
    ) -> RetrievalResult:
        """Lexical-only retrieval (deterministic, no adapter)."""
        return self._assemble(query, options or RetrievalOptions(), None, transcript_tokens)

    async def retrieve(
        self,
        query: str,
        adapter: LLMInterface | None = None,
        options: RetrievalOptions | None = None,
        *,
        transcript_tokens: int | None = None,
    ) -> RetrievalResult:
        """Hybrid retrieval when embeddings are ready, lexical otherwise."""
        options = options or RetrievalOptions()
        embedding_scores: dict[str, float] | None = None
        if (
            self._embeddings_ready
            and adapter is not None
            and self._store is not None
            and query.strip()
        ):
            try:
                q_emb = await adapter.embed(query)
                hits = self._store.search(q_emb, top_k=len(self._items))
                embedding_scores = {item_id: max(0.0, min(1.0, sim)) for item_id, sim in hits}
            except Exception as exc:
                logger.warning(
                    "Query embedding failed (%s) — using lexical scores only",
                    exc.__class__.__name__,
                )
                embedding_scores = None
        return self._assemble(query, options, embedding_scores, transcript_tokens)

    def _assemble(
        self,
        query: str,
        options: RetrievalOptions,
        embedding_scores: dict[str, float] | None,
        transcript_tokens: int | None,
    ) -> RetrievalResult:
        items = self._items
        total = len(items)
        tokens_full = sum(estimate_tokens(i.content) for i in items)
        method = "hybrid" if embedding_scores is not None else "lexical"
        result = RetrievalResult(
            query=query,
            method=method,
            options=options,
            total_items=total,
            tokens_full_memory=tokens_full,
            tokens_transcript=transcript_tokens,
        )
        if not items or self._lexical is None:
            return result

        has_query = bool(tokenize(query))
        lexical = self._lexical.score(query)
        allowed = set(options.categories) if options.categories else None

        candidates: list[ScoredItem] = []
        for idx, item in enumerate(items):
            if allowed is not None and item.category not in allowed:
                continue
            if not options.include_inactive and not item.is_active:
                result.excluded_inactive += 1
                continue
            lex_score, matched = lexical[idx]
            emb_score = embedding_scores.get(item.id) if embedding_scores else None
            if not has_query:
                score = item.confidence
                reason = "No query given — ranked by extraction confidence"
            elif emb_score is not None:
                w = self._embedding_weight
                score = (1 - w) * lex_score + w * emb_score
                reason = _reason(matched, lex_score, emb_score)
            else:
                score = lex_score
                reason = _reason(matched, lex_score, None)
            candidates.append(
                ScoredItem(
                    item=item,
                    rank=0,
                    score=round(min(1.0, max(0.0, score)), 4),
                    lexical_score=round(lex_score, 4),
                    embedding_score=None if emb_score is None else round(emb_score, 4),
                    matched_terms=matched,
                    reason=reason,
                    tokens=estimate_tokens(item.content),
                )
            )

        result.considered = len(candidates)
        candidates.sort(
            key=lambda s: (
                -s.score,
                -s.item.confidence,
                _CATEGORY_ORDER[s.item.category],
                s.item.id,
            )
        )

        selected: list[ScoredItem] = []
        used_tokens = 0
        for cand in candidates:
            if has_query and cand.score < options.min_score:
                result.dropped_below_min_score += 1
                continue
            if has_query and cand.score <= 0.0 and options.min_score <= 0.0:
                # Items with no evidence are never "relevant"; keep them out of the
                # selection but do not count them as threshold drops.
                continue
            if len(selected) >= options.top_k:
                break
            if (
                options.token_budget is not None
                and used_tokens + cand.tokens > options.token_budget
            ):
                result.dropped_for_budget += 1
                continue
            used_tokens += cand.tokens
            cand.rank = len(selected) + 1
            selected.append(cand)

        result.selected = selected
        result.tokens_selected = used_tokens
        logger.info(
            "Retrieval: %d/%d items selected (method=%s, top_k=%d, budget=%s)",
            len(selected),
            result.considered,
            method,
            options.top_k,
            options.token_budget,
        )
        return result

    # -- Backward-compatible helpers ----------------------------------------

    async def query(
        self,
        user_query: str,
        adapter: LLMInterface | None = None,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[MemoryItem]:
        """Return only the selected items (legacy API)."""
        if not self.is_indexed:
            logger.warning("Memory not indexed — returning empty results")
            return []
        result = await self.retrieve(
            user_query, adapter, RetrievalOptions(top_k=top_k, min_score=min_score)
        )
        return [s.item for s in result.selected]

    async def query_and_filter(
        self,
        user_query: str,
        memory: StructuredMemory,
        adapter: LLMInterface | None = None,
        *,
        top_k: int = 5,
    ) -> StructuredMemory:
        """Return *memory* filtered to the selected items (legacy API)."""
        relevant = await self.query(user_query, adapter, top_k=top_k)
        keep = {item.id for item in relevant}
        filtered = StructuredMemory()
        for cat in MemoryCategory:
            filtered.set_category(cat, [i for i in memory.get_category(cat) if i.id in keep])
        return filtered


def _reason(matched: list[str], lex: float, emb: float | None) -> str:
    if matched:
        terms = ", ".join(f"'{t}'" for t in matched[:6])
        text = f"Matched {terms} (lexical {lex:.2f})"
    else:
        text = "No query terms matched"
    if emb is not None:
        text += f"; embedding similarity {emb:.2f}"
    return text
