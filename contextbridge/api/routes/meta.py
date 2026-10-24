"""Health, readiness, evaluation, and local-model discovery."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from contextbridge import __version__, telemetry
from contextbridge.api.deps import ServiceDep, SettingsDep
from contextbridge.api.schemas import HealthResponse, ReadinessResponse
from contextbridge.core.redaction import describe_kinds
from contextbridge.models import ItemStatus, MemoryCategory, SharingPolicy

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthResponse, summary="Service configuration and health")
def health(svc: ServiceDep, settings: SettingsDep) -> HealthResponse:
    store = svc.store
    return HealthResponse(
        ok=True,
        version=__version__,
        environment=settings.environment,
        storage_backend=store.backend_name,
        storage_location=store.location(),
        redact_by_default=settings.redact_by_default,
        redaction_kinds=describe_kinds(),
        categories=[c.value for c in MemoryCategory],
        sharing_policies=[p.value for p in SharingPolicy],
        item_statuses=[s.value for s in ItemStatus],
        max_upload_mb=settings.max_upload_bytes // (1024 * 1024),
        api_key_required=settings.api_key_required,
        tracing=telemetry.enabled(),
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
def ready(svc: ServiceDep) -> ReadinessResponse:
    """``200`` when the storage backend answers, ``503`` otherwise (see app-level handler)."""
    store = svc.store
    checks: dict[str, bool] = {}
    ping = getattr(store, "ping", None)
    if callable(ping):
        checks["storage"] = bool(ping())
    else:
        try:
            store.list_packages()
            checks["storage"] = True
        except Exception:
            checks["storage"] = False
    return ReadinessResponse(
        ready=all(checks.values()), storage_backend=store.backend_name, checks=checks
    )


@router.get("/eval", summary="Run the offline evaluation suite")
def evaluation() -> dict[str, Any]:
    from contextbridge.evaluation import run_evaluation

    return run_evaluation().model_dump(mode="json")


@router.get("/eval/golden", summary="Run the golden retrieval harness")
def golden(top_k: int = 3) -> dict[str, Any]:
    from contextbridge.evaluation.golden import run_golden

    return run_golden(top_k=max(1, min(top_k, 20))).model_dump(mode="json")


@router.get("/local/models", summary="Models available from a local Ollama server")
def local_models() -> dict[str, Any]:
    try:
        from contextbridge.adapters.local_adapter import LocalAdapter
        from contextbridge.service import _run

        return {"models": _run(LocalAdapter.list_models())}
    except Exception:
        return {"models": [], "error": "Ollama unreachable"}
