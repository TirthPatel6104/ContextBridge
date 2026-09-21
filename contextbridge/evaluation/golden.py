"""
Golden retrieval harness: a fixed set of (query → relevant items) pairs that
runs on every CI build and fails the build when retrieval quality regresses.

Why a second harness next to :mod:`contextbridge.evaluation.runner`?  The
runner scores every component on three short transcripts — enough to spot a
broken rule, too small to measure retrieval.  The golden set is retrieval
only: several realistic memory sets (one per persona) and 80+ hand-written
queries labelled with the items a good retriever must surface, tagged by how
hard they are:

``lexical``     the query shares clear vocabulary with the relevant item
``paraphrase``  the query is reworded; some stems still overlap
``semantic``    no meaningful vocabulary overlap — BM25 is expected to miss
                these; they exist to measure the headroom embeddings buy
``multi``       more than one item is relevant

The report carries per-tag and overall Hit@1, Precision@k, Recall@k, MRR
and nDCG@k.  ``compare_to_baseline`` diffs a report against the published
``baseline.json`` and returns every metric that dropped by more than the
tolerance, which is what ``cb eval --golden --check-baseline`` and CI use.

Everything is deterministic and offline; the optional ``adapter`` argument
lets the same harness score hybrid (embedding-blended) retrieval.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable
from importlib import resources
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field

from contextbridge.core.retriever import MemoryRetriever
from contextbridge.models import (
    MemoryCategory,
    MemoryItem,
    RetrievalOptions,
    StructuredMemory,
    make_item_id,
)

FIXTURE_PACKAGE = "contextbridge.evaluation.fixtures"
GOLDEN_FILE = "golden_pairs.json"
BASELINE_FILE = "golden_baseline.json"

#: Aggregate metrics that must not regress (lower is never better for these).
GUARDED_METRICS: tuple[str, ...] = ("hit_at_1", "precision_at_k", "recall_at_k", "mrr", "ndcg_at_k")


class GoldenPairResult(BaseModel):
    id: str
    memory_set: str
    query: str
    tags: list[str]
    relevant: list[str]
    ranked: list[str]
    hit_at_1: float
    precision_at_k: float
    recall_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float


class GoldenReport(BaseModel):
    version: int = 1
    top_k: int
    method: str
    pairs: int
    memory_sets: dict[str, int]
    aggregate: dict[str, float]
    by_tag: dict[str, dict[str, float]]
    results: list[GoldenPairResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def failures(self) -> list[GoldenPairResult]:
        return [r for r in self.results if r.reciprocal_rank == 0.0]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_golden(path: str | Path | None = None) -> dict[str, Any]:
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    with resources.files(FIXTURE_PACKAGE).joinpath(GOLDEN_FILE).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_baseline(path: str | Path | None = None) -> dict[str, Any] | None:
    if path is not None:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    ref = resources.files(FIXTURE_PACKAGE).joinpath(BASELINE_FILE)
    if not ref.is_file():
        return None
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _memory_from_set(items: Iterable[dict[str, Any]], origin: str) -> StructuredMemory:
    return StructuredMemory.from_items(
        [
            MemoryItem(
                category=MemoryCategory(it["category"]),
                content=it["content"],
                confidence=float(it.get("confidence", 1.0)),
                source=it.get("source", ""),
                origin=origin,
            )
            for it in items
        ]
    )


def validate_golden(data: dict[str, Any]) -> list[str]:
    """Structural checks; returns human-readable problems (empty when valid)."""
    problems: list[str] = []
    sets = data.get("memory_sets") or {}
    if not sets:
        problems.append("no memory_sets")
    for name, items in sets.items():
        if not isinstance(items, list) or not items:
            problems.append(f"memory set {name!r} is empty")
            continue
        for i, it in enumerate(items):
            try:
                MemoryCategory(it["category"])
            except Exception:
                problems.append(f"{name}[{i}] has an invalid category")
            if not str(it.get("content", "")).strip():
                problems.append(f"{name}[{i}] has empty content")
    seen: set[str] = set()
    for pair in data.get("pairs") or []:
        pid = pair.get("id")
        if not pid or pid in seen:
            problems.append(f"pair id missing or duplicated: {pid!r}")
        seen.add(pid or "")
        if pair.get("set") not in sets:
            problems.append(f"pair {pid}: unknown memory set {pair.get('set')!r}")
            continue
        n = len(sets[pair["set"]])
        rel = pair.get("relevant") or []
        if not rel:
            problems.append(f"pair {pid}: no relevant items")
        if any(not isinstance(r, int) or r < 0 or r >= n for r in rel):
            problems.append(f"pair {pid}: relevant index out of range")
        if not str(pair.get("query", "")).strip():
            problems.append(f"pair {pid}: empty query")
    return problems


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _ndcg(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1 / math.log2(pos + 1) for pos, rid in enumerate(ranked[:k], 1) if rid in relevant)
    ideal = sum(1 / math.log2(pos + 1) for pos in range(1, min(k, len(relevant)) + 1))
    return dcg / ideal if ideal else 0.0


def _aggregate(results: list[GoldenPairResult]) -> dict[str, float]:
    if not results:
        return {m: 0.0 for m in GUARDED_METRICS}
    return {
        "hit_at_1": round(mean(r.hit_at_1 for r in results), 4),
        "precision_at_k": round(mean(r.precision_at_k for r in results), 4),
        "recall_at_k": round(mean(r.recall_at_k for r in results), 4),
        "mrr": round(mean(r.reciprocal_rank for r in results), 4),
        "ndcg_at_k": round(mean(r.ndcg_at_k for r in results), 4),
    }


def run_golden(
    *,
    top_k: int = 3,
    adapter: Any | None = None,
    data: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> GoldenReport:
    """Score lexical (or hybrid, with *adapter*) retrieval on the golden pairs."""
    data = data or load_golden(path)
    problems = validate_golden(data)
    if problems:
        raise ValueError("Invalid golden set: " + "; ".join(problems[:5]))

    sets = data["memory_sets"]
    retrievers: dict[str, MemoryRetriever] = {}
    ids_by_set: dict[str, list[str]] = {}
    method = "lexical"
    for name, items in sets.items():
        memory = _memory_from_set(items, origin=name)
        ids_by_set[name] = [make_item_id(it["category"], it["content"]) for it in items]
        if adapter is not None:
            from contextbridge.service import _run
            from contextbridge.storage.vector_store import VectorStore

            retriever = MemoryRetriever(VectorStore())
            _run(retriever.index(memory, adapter))
            if retriever.uses_embeddings:
                method = "hybrid"
        else:
            retriever = MemoryRetriever()
            retriever.index_sync(memory)
        retrievers[name] = retriever

    results: list[GoldenPairResult] = []
    for pair in data["pairs"]:
        set_name = pair["set"]
        relevant = {ids_by_set[set_name][i] for i in pair["relevant"]}
        retriever = retrievers[set_name]
        options = RetrievalOptions(top_k=top_k)
        if adapter is not None:
            from contextbridge.service import _run

            result = _run(retriever.retrieve(pair["query"], adapter, options))
        else:
            result = retriever.retrieve_sync(pair["query"], options)
        ranked = [s.item.id for s in result.selected]
        hits = sum(1 for rid in ranked if rid in relevant)
        rr = next((1 / pos for pos, rid in enumerate(ranked, 1) if rid in relevant), 0.0)
        results.append(
            GoldenPairResult(
                id=pair["id"],
                memory_set=set_name,
                query=pair["query"],
                tags=list(pair.get("tags") or []),
                relevant=sorted(relevant),
                ranked=ranked,
                hit_at_1=1.0 if ranked and ranked[0] in relevant else 0.0,
                precision_at_k=round(hits / top_k, 4),
                recall_at_k=round(hits / len(relevant), 4),
                reciprocal_rank=round(rr, 4),
                ndcg_at_k=round(_ndcg(ranked, relevant, top_k), 4),
            )
        )

    by_tag: dict[str, list[GoldenPairResult]] = defaultdict(list)
    for r in results:
        for tag in r.tags or ["untagged"]:
            by_tag[tag].append(r)

    return GoldenReport(
        top_k=top_k,
        method=method,
        pairs=len(results),
        memory_sets={name: len(items) for name, items in sets.items()},
        aggregate=_aggregate(results),
        by_tag={
            tag: {**_aggregate(rs), "pairs": float(len(rs))} for tag, rs in sorted(by_tag.items())
        },
        results=results,
        notes=[
            f"{len(results)} golden pairs over {len(sets)} memory sets; top_k={top_k}.",
            "'semantic' pairs share no vocabulary with their answer and are expected to "
            "fail under lexical retrieval; they measure the headroom embeddings add.",
            "Scores are computed over hand-labelled memory, not extracted memory.",
        ],
    )


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------


def compare_to_baseline(
    report: GoldenReport, baseline: dict[str, Any], *, tolerance: float = 0.02
) -> list[dict[str, Any]]:
    """Metrics that regressed beyond *tolerance*: ``[{metric, baseline, current, delta}]``."""
    regressions: list[dict[str, Any]] = []
    base_agg = baseline.get("aggregate") or {}
    for metric in GUARDED_METRICS:
        if metric not in base_agg:
            continue
        current = report.aggregate.get(metric, 0.0)
        delta = round(current - float(base_agg[metric]), 4)
        if delta < -tolerance:
            regressions.append(
                {
                    "metric": metric,
                    "baseline": float(base_agg[metric]),
                    "current": current,
                    "delta": delta,
                }
            )
    base_pairs = baseline.get("pairs")
    if isinstance(base_pairs, int) and report.pairs < base_pairs:
        regressions.append(
            {"metric": "pairs", "baseline": base_pairs, "current": report.pairs, "delta": 0.0}
        )
    return regressions


def baseline_from_report(report: GoldenReport) -> dict[str, Any]:
    """The subset of a report that is stored as the published baseline."""
    return {
        "version": report.version,
        "top_k": report.top_k,
        "method": report.method,
        "pairs": report.pairs,
        "aggregate": report.aggregate,
        "by_tag": report.by_tag,
    }


__all__ = [
    "GoldenPairResult",
    "GoldenReport",
    "GUARDED_METRICS",
    "load_golden",
    "load_baseline",
    "validate_golden",
    "run_golden",
    "compare_to_baseline",
    "baseline_from_report",
]
