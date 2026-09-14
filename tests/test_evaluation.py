"""Tests for the offline evaluation suite."""

from __future__ import annotations

from contextbridge.evaluation import run_evaluation


def test_evaluation_is_deterministic_and_bounded():
    first = run_evaluation()
    second = run_evaluation()
    assert first.model_dump() == second.model_dump()
    assert first.fixtures == ["backend_migration", "thesis_research", "product_launch"]
    for key, value in first.aggregate.items():
        if key == "redaction_false_positives":
            assert value >= 0
        else:
            assert 0.0 <= value <= 1.0, key


def test_evaluation_sanity_floors():
    """Guard rails so a regression in the offline components is noticed."""
    report = run_evaluation()
    assert report.aggregate["retrieval_mrr"] >= 0.7
    assert report.aggregate["redaction_recall"] >= 0.9
    assert report.aggregate["redaction_false_positives"] == 0
    assert report.aggregate["duplicate_f1"] >= 0.7
    assert report.aggregate["token_savings_vs_transcript"] > 0.5
    assert report.notes


def test_retrieval_scores_reference_gold_items():
    report = run_evaluation(top_k=1)
    assert all(r.top_k == 1 for r in report.retrieval)
    perfect = [r for r in report.retrieval if r.reciprocal_rank == 1.0]
    assert perfect, "at least one query should rank its gold item first"
