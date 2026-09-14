"""Offline evaluation suite (fixtures only — no vendor API calls)."""

from contextbridge.evaluation.runner import EvaluationReport, run_evaluation

__all__ = ["EvaluationReport", "run_evaluation"]
