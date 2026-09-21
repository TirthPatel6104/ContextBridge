"""Tests for the golden retrieval harness and its baseline gate."""

from __future__ import annotations

import json

import pytest

from contextbridge.evaluation.golden import (
    GUARDED_METRICS,
    baseline_from_report,
    compare_to_baseline,
    load_baseline,
    load_golden,
    run_golden,
    validate_golden,
)
from tests.conftest import MockAdapter


def test_golden_set_is_well_formed_and_large_enough():
    data = load_golden()
    assert validate_golden(data) == []
    assert 50 <= len(data["pairs"]) <= 100
    tags = {t for p in data["pairs"] for t in p["tags"]}
    assert {"lexical", "paraphrase", "semantic", "multi"} <= tags


def test_run_is_deterministic_and_bounded():
    a = run_golden()
    b = run_golden()
    assert a.model_dump() == b.model_dump()
    assert a.method == "lexical" and a.pairs == len(a.results)
    for metric in GUARDED_METRICS:
        assert 0.0 <= a.aggregate[metric] <= 1.0
    assert a.by_tag["lexical"]["hit_at_1"] >= 0.9
    assert a.by_tag["semantic"]["mrr"] < a.by_tag["lexical"]["mrr"]
    assert a.aggregate["mrr"] >= 0.75
    assert all(f.reciprocal_rank == 0.0 for f in a.failures())


def test_published_baseline_matches_current_behaviour():
    baseline = load_baseline()
    assert baseline is not None, "golden_baseline.json must ship with the package"
    report = run_golden(top_k=baseline["top_k"])
    assert compare_to_baseline(report, baseline) == []
    assert baseline_from_report(report)["aggregate"] == baseline["aggregate"]


def test_compare_detects_regressions():
    report = run_golden()
    inflated = json.loads(json.dumps(baseline_from_report(report)))
    inflated["aggregate"]["mrr"] = 0.99
    inflated["pairs"] = report.pairs + 1
    regressions = compare_to_baseline(report, inflated, tolerance=0.01)
    assert {r["metric"] for r in regressions} == {"mrr", "pairs"}
    assert compare_to_baseline(report, {"aggregate": {}}) == []


def test_hybrid_mode_with_mock_adapter():
    report = run_golden(top_k=2, adapter=MockAdapter())
    assert report.method == "hybrid" and report.top_k == 2
    # Hash embeddings are noise; blended scores must still be valid numbers.
    for metric in GUARDED_METRICS:
        assert 0.0 <= report.aggregate[metric] <= 1.0


def test_invalid_golden_data_is_rejected(tmp_path):
    bad = {
        "memory_sets": {"s": [{"category": "facts", "content": "x"}, {"category": "nope"}]},
        "pairs": [
            {"id": "a", "set": "s", "query": "q", "relevant": [5]},
            {"id": "a", "set": "missing", "query": "", "relevant": []},
        ],
    }
    problems = validate_golden(bad)
    assert any("invalid category" in p for p in problems)
    assert any("out of range" in p for p in problems)
    assert any("duplicated" in p for p in problems)
    with pytest.raises(ValueError):
        run_golden(data=bad)
    path = tmp_path / "g.json"
    path.write_text(json.dumps(load_golden()), encoding="utf-8")
    assert run_golden(path=path).pairs >= 50
    assert load_baseline(tmp_path / "none.json") is None
