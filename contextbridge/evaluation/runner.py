"""
Reproducible, offline evaluation of ContextBridge's core behaviours.

Everything here runs against JSON fixtures shipped with the package and the
deterministic components (rule-based extractor, lexical retriever, merger,
redactor).  No network, no API keys, no randomness — the numbers are the
same on every machine, which is the point: they document *what the offline
path actually does*, not what a vendor model might do.

Metrics
-------
extraction
    Coverage of hand-labelled gold items by the rule-based extractor
    (a gold item counts as covered when one extracted item contains all of
    its keywords) and the share of extracted items that match no gold item.
retrieval
    Precision@k, Recall@k and MRR of lexical retrieval over the gold memory
    for each fixture query.
duplicates
    Precision / recall / F1 of the merger's duplicate decision on labelled
    pairs.
redaction
    Recall of planted secrets/PII by kind, plus false positives on clean text.
tokens
    Estimated tokens of the transcript vs. the full gold memory vs. the
    retrieval selection (top-k) — i.e. how much a user saves.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field

from contextbridge.core.local_extractor import LocalExtractor
from contextbridge.core.merger import PackageMerger, similarity
from contextbridge.core.redaction import scan_text
from contextbridge.core.retriever import MemoryRetriever
from contextbridge.core.tokens import estimate_tokens
from contextbridge.models import (
    MemoryCategory,
    MemoryItem,
    RetrievalOptions,
    StructuredMemory,
    make_item_id,
)

FIXTURE_PACKAGE = "contextbridge.evaluation.fixtures"


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


class ExtractionScore(BaseModel):
    fixture: str
    gold_items: int
    covered: int
    coverage: float
    extracted_items: int
    unmatched_extracted: int
    missed: list[str] = Field(default_factory=list)


class RetrievalScore(BaseModel):
    fixture: str
    query: str
    top_k: int
    relevant: int
    hits: int
    precision_at_k: float
    recall_at_k: float
    reciprocal_rank: float
    selected: list[str] = Field(default_factory=list)


class TokenScore(BaseModel):
    fixture: str
    tokens_transcript: int
    tokens_full_memory: int
    tokens_selected_mean: float
    savings_vs_transcript: float
    savings_vs_full_memory: float


class DuplicateScore(BaseModel):
    pairs: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    threshold: float
    errors: list[dict[str, Any]] = Field(default_factory=list)


class RedactionScore(BaseModel):
    planted: int
    detected: int
    recall: float
    recall_by_kind: dict[str, float]
    clean_samples: int
    false_positives: int
    misses: list[dict[str, str]] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    version: int = 1
    fixtures: list[str]
    extraction: list[ExtractionScore]
    retrieval: list[RetrievalScore]
    tokens: list[TokenScore]
    duplicates: DuplicateScore
    redaction: RedactionScore
    aggregate: dict[str, float]
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_json(name: str) -> Any:
    with resources.files(FIXTURE_PACKAGE).joinpath(name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_transcript_fixtures() -> list[dict[str, Any]]:
    index = _load_json("index.json")
    return [_load_json(fname) for fname in index["transcripts"]]


def _gold_memory(fixture: dict[str, Any]) -> StructuredMemory:
    items = [
        MemoryItem(
            category=MemoryCategory(g["category"]),
            content=g["content"],
            confidence=1.0,
            origin=fixture["id"],
        )
        for g in fixture["gold_items"]
    ]
    return StructuredMemory.from_items(items)


def _contains_all(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return all(k.lower() in lowered for k in keywords)


# ---------------------------------------------------------------------------
# Metric computations
# ---------------------------------------------------------------------------


def evaluate_extraction(fixture: dict[str, Any]) -> ExtractionScore:
    extracted = LocalExtractor().extract(fixture["transcript"])
    extracted_items = extracted.all_items
    covered = 0
    missed: list[str] = []
    matched_extracted: set[str] = set()
    for gold in fixture["gold_items"]:
        hit = False
        for item in extracted_items:
            if _contains_all(item.content, gold["keywords"]):
                hit = True
                matched_extracted.add(item.id)
        if hit:
            covered += 1
        else:
            missed.append(gold["content"])
    gold_n = len(fixture["gold_items"])
    return ExtractionScore(
        fixture=fixture["id"],
        gold_items=gold_n,
        covered=covered,
        coverage=round(covered / gold_n, 4) if gold_n else 0.0,
        extracted_items=len(extracted_items),
        unmatched_extracted=len(extracted_items) - len(matched_extracted),
        missed=missed,
    )


def evaluate_retrieval(fixture: dict[str, Any], *, top_k: int = 3) -> list[RetrievalScore]:
    memory = _gold_memory(fixture)
    # Ids in *fixture order* (relevant_gold indexes refer to the gold_items list).
    gold_ids = [make_item_id(g["category"], g["content"]) for g in fixture["gold_items"]]
    retriever = MemoryRetriever()
    retriever.index_sync(memory)
    scores: list[RetrievalScore] = []
    for q in fixture["queries"]:
        relevant = {gold_ids[i] for i in q["relevant_gold"]}
        result = retriever.retrieve_sync(q["query"], RetrievalOptions(top_k=top_k))
        ranked = [s.item.id for s in result.selected]
        hits = sum(1 for rid in ranked if rid in relevant)
        rr = 0.0
        for pos, rid in enumerate(ranked, 1):
            if rid in relevant:
                rr = 1 / pos
                break
        scores.append(
            RetrievalScore(
                fixture=fixture["id"],
                query=q["query"],
                top_k=top_k,
                relevant=len(relevant),
                hits=hits,
                precision_at_k=round(hits / max(1, min(top_k, len(ranked) or top_k)), 4),
                recall_at_k=round(hits / len(relevant), 4) if relevant else 0.0,
                reciprocal_rank=round(rr, 4),
                selected=[s.item.content for s in result.selected],
            )
        )
    return scores


def evaluate_tokens(fixture: dict[str, Any], *, top_k: int = 3) -> TokenScore:
    memory = _gold_memory(fixture)
    retriever = MemoryRetriever()
    retriever.index_sync(memory)
    transcript_tokens = estimate_tokens(fixture["transcript"])
    full_tokens = sum(estimate_tokens(i.content) for i in memory.all_items)
    selected = [
        retriever.retrieve_sync(q["query"], RetrievalOptions(top_k=top_k)).tokens_selected
        for q in fixture["queries"]
    ]
    sel_mean = mean(selected) if selected else 0.0
    return TokenScore(
        fixture=fixture["id"],
        tokens_transcript=transcript_tokens,
        tokens_full_memory=full_tokens,
        tokens_selected_mean=round(sel_mean, 1),
        savings_vs_transcript=round(1 - sel_mean / transcript_tokens, 4)
        if transcript_tokens
        else 0.0,
        savings_vs_full_memory=round(1 - sel_mean / full_tokens, 4) if full_tokens else 0.0,
    )


def evaluate_duplicates() -> DuplicateScore:
    data = _load_json("duplicates.json")
    merger = PackageMerger()
    tp = fp = fn = 0
    errors: list[dict[str, Any]] = []
    for pair in data["pairs"]:
        sim = similarity(pair["a"], pair["b"])
        predicted = sim >= merger.duplicate_threshold
        expected = bool(pair["duplicate"])
        if predicted and expected:
            tp += 1
        elif predicted and not expected:
            fp += 1
            errors.append({"a": pair["a"], "b": pair["b"], "similarity": sim, "kind": "fp"})
        elif not predicted and expected:
            fn += 1
            errors.append({"a": pair["a"], "b": pair["b"], "similarity": sim, "kind": "fn"})
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return DuplicateScore(
        pairs=len(data["pairs"]),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        threshold=merger.duplicate_threshold,
        errors=errors,
    )


def evaluate_redaction() -> RedactionScore:
    data = _load_json("redaction.json")
    planted = detected = 0
    by_kind_total: dict[str, int] = {}
    by_kind_hit: dict[str, int] = {}
    misses: list[dict[str, str]] = []
    for sample in data["positives"]:
        found_kinds = {f.kind for f in scan_text(sample["text"])}
        for kind in sample["expected_kinds"]:
            planted += 1
            by_kind_total[kind] = by_kind_total.get(kind, 0) + 1
            if kind in found_kinds:
                detected += 1
                by_kind_hit[kind] = by_kind_hit.get(kind, 0) + 1
            else:
                misses.append({"kind": kind, "text": sample["text"][:80]})
    false_positives = 0
    for sample in data["negatives"]:
        if scan_text(sample["text"]):
            false_positives += 1
            misses.append({"kind": "false_positive", "text": sample["text"][:80]})
    return RedactionScore(
        planted=planted,
        detected=detected,
        recall=round(detected / planted, 4) if planted else 0.0,
        recall_by_kind={
            k: round(by_kind_hit.get(k, 0) / n, 4) for k, n in sorted(by_kind_total.items())
        },
        clean_samples=len(data["negatives"]),
        false_positives=false_positives,
        misses=misses,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_evaluation(*, top_k: int = 3) -> EvaluationReport:
    """Run every metric and return a structured report."""
    fixtures = load_transcript_fixtures()
    extraction = [evaluate_extraction(f) for f in fixtures]
    retrieval = [s for f in fixtures for s in evaluate_retrieval(f, top_k=top_k)]
    tokens = [evaluate_tokens(f, top_k=top_k) for f in fixtures]
    duplicates = evaluate_duplicates()
    redaction = evaluate_redaction()

    aggregate = {
        "extraction_coverage": round(mean(e.coverage for e in extraction), 4),
        "extraction_unmatched_ratio": round(
            sum(e.unmatched_extracted for e in extraction)
            / max(1, sum(e.extracted_items for e in extraction)),
            4,
        ),
        "retrieval_precision_at_k": round(mean(r.precision_at_k for r in retrieval), 4),
        "retrieval_recall_at_k": round(mean(r.recall_at_k for r in retrieval), 4),
        "retrieval_mrr": round(mean(r.reciprocal_rank for r in retrieval), 4),
        "duplicate_f1": duplicates.f1,
        "redaction_recall": redaction.recall,
        "redaction_false_positives": float(redaction.false_positives),
        "token_savings_vs_transcript": round(mean(t.savings_vs_transcript for t in tokens), 4),
        "token_savings_vs_full_memory": round(mean(t.savings_vs_full_memory for t in tokens), 4),
    }
    notes = [
        "Extraction is measured for the offline rule-based extractor only; "
        "LLM extractors are not evaluated here because that would require API calls.",
        "Retrieval is evaluated over hand-labelled gold memory so that extraction "
        "errors do not leak into retrieval scores.",
        "Token counts are heuristic estimates (see contextbridge.core.tokens).",
        f"Retrieval metrics use top_k={top_k}.",
    ]
    return EvaluationReport(
        fixtures=[f["id"] for f in fixtures],
        extraction=extraction,
        retrieval=retrieval,
        tokens=tokens,
        duplicates=duplicates,
        redaction=redaction,
        aggregate=aggregate,
        notes=notes,
    )
