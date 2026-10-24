"""Package CRUD, review, lifecycle, sharing, ledger, health, portability, merge."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request, Response

from contextbridge.api.deps import ServiceDep
from contextbridge.api.schemas import (
    IdsRequest,
    MergeRequest,
    PackageListResponse,
    ResolveConflictRequest,
    RollbackRequest,
    SharingRequest,
    StatusRequest,
    SupersedeRequest,
)
from contextbridge.validation import ValidationError, safe_filename, validate_package_name

router = APIRouter(prefix="/packages", tags=["packages"])


def _item_dict(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "category": item.category.value,
        "content": item.content,
        "confidence": item.confidence,
        "source": item.source,
        "origin": item.origin,
    }


# -- Listing / detail --------------------------------------------------------


@router.get("", response_model=PackageListResponse, summary="List packages")
def list_packages(svc: ServiceDep) -> dict[str, Any]:
    return {"packages": svc.list_packages()}


# NB: the static routes ``/import`` and ``/merge`` must be declared before
# ``/{name}`` so they are not captured as package names.


@router.post("/import", summary="Import a portable package file")
async def import_package(request: Request, svc: ServiceDep) -> dict[str, Any]:
    """Send either multipart (``file``, optional ``rename`` / ``overwrite``) or a JSON body."""
    rename: str | None = None
    overwrite = False
    content_type = request.headers.get("content-type", "")
    data: bytes | dict[str, Any]
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", None):
            raise ValidationError("Send a package file as 'file' or a JSON body")
        safe_filename(upload.filename)
        data = await upload.read()
        rename = str(form.get("rename") or "") or None
        overwrite = str(form.get("overwrite") or "").lower() in {"1", "true", "yes", "on"}
    else:
        try:
            payload = await request.json()
        except Exception as exc:
            raise ValidationError("Send a package file as 'file' or a JSON body") from exc
        if not isinstance(payload, dict):
            raise ValidationError("Send a package file as 'file' or a JSON body")
        data = payload.get("package_file") or payload
        rename = payload.get("rename") or None
        overwrite = bool(payload.get("overwrite"))
    pkg = svc.import_package_data(data, rename=rename, overwrite=overwrite)
    return {
        "success": True,
        "package_name": pkg.name,
        "version": pkg.version,
        "total_items": pkg.memory.total_items,
    }


@router.post("/merge", summary="Merge packages with duplicate folding and conflict flags")
def merge(body: MergeRequest, svc: ServiceDep) -> dict[str, Any]:
    merged, result = svc.merge_packages(
        [str(n) for n in body.names],
        body.new_name,
        dry_run=body.dry_run,
        overwrite=body.overwrite,
    )
    return {
        "success": True,
        "dry_run": body.dry_run,
        "package_name": merged.name,
        "summary": result.summary(),
        "memory": svc.memory_to_dict(result.memory),
        "duplicates": [
            {
                "kept": _item_dict(g.kept),
                "duplicates": [_item_dict(d) for d in g.duplicates],
                "similarity": g.similarity,
                "origins": g.origins,
            }
            for g in result.duplicates
        ],
        "conflicts": [
            {
                "category": c.category.value,
                "a": _item_dict(c.a),
                "b": _item_dict(c.b),
                "similarity": c.similarity,
                "shared_terms": c.shared_terms,
                "reason": c.reason,
            }
            for c in result.conflicts
        ],
    }


@router.get("/{name}", summary="Package detail with memory, history, conflicts")
def get_package(name: str, svc: ServiceDep) -> dict[str, Any]:
    return svc.package_to_dict(svc.get_package(name))


@router.delete("/{name}", summary="Delete a package and its ledger")
def delete_package(name: str, svc: ServiceDep) -> dict[str, Any]:
    svc.delete_package(name)
    return {"success": True, "deleted": name}


@router.get("/{name}/history", summary="Version history")
def history(name: str, svc: ServiceDep) -> dict[str, Any]:
    return {"package_name": name, "history": svc.history(name)}


@router.post("/{name}/rollback", summary="Roll back to an earlier version")
def rollback(name: str, body: RollbackRequest, svc: ServiceDep) -> dict[str, Any]:
    try:
        pkg = svc.rollback(name, body.version)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return {"success": True, "package_name": pkg.name, "version": pkg.version}


# -- Review ------------------------------------------------------------------


@router.post("/{name}/items/remove", summary="Remove items (versioned, reversible)")
def remove_items(name: str, body: IdsRequest, svc: ServiceDep) -> dict[str, Any]:
    pkg = svc.remove_items(name, body.ids, note=body.note)
    return {
        "success": True,
        "package_name": pkg.name,
        "version": pkg.version,
        "total_items": pkg.memory.total_items,
        "memory": svc.memory_to_dict(pkg.memory),
    }


@router.post("/{name}/items/sharing", summary="Set who may see items (any / local_only / never)")
def set_sharing(name: str, body: SharingRequest, svc: ServiceDep) -> dict[str, Any]:
    pkg, changed = svc.set_sharing(name, body.ids, body.policy)
    return {"success": True, "package_name": pkg.name, "version": pkg.version, "changed": changed}


@router.post("/{name}/items/status", summary="Mark items done or active")
def set_status(name: str, body: StatusRequest, svc: ServiceDep) -> dict[str, Any]:
    pkg, changed = svc.set_status(name, body.ids, body.status)
    return {"success": True, "package_name": pkg.name, "version": pkg.version, "changed": changed}


@router.post("/{name}/items/supersede", summary="Record that one item replaces another")
def supersede(name: str, body: SupersedeRequest, svc: ServiceDep) -> dict[str, Any]:
    pkg = svc.supersede(name, body.old_id, body.new_id)
    return {"success": True, "package_name": pkg.name, "version": pkg.version}


@router.get("/{name}/conflicts", summary="Statements that may contradict stored memory")
def conflicts(name: str, svc: ServiceDep) -> dict[str, Any]:
    detail = svc.package_to_dict(svc.get_package(name))
    return {"package_name": name, "conflicts": detail["pending_conflicts"]}


@router.post("/{name}/conflicts/resolve", summary="Settle a pending conflict")
def resolve_conflict(name: str, body: ResolveConflictRequest, svc: ServiceDep) -> dict[str, Any]:
    pkg = svc.resolve_conflict(name, body.existing_id, body.incoming_id, body.action)
    return {
        "success": True,
        "package_name": pkg.name,
        "version": pkg.version,
        "remaining": len(svc.pending_conflicts(name)),
    }


# -- Ledger & health ---------------------------------------------------------


@router.get("/{name}/audit", summary="Egress ledger: which items went to which model")
def audit(name: str, svc: ServiceDep, limit: int = Query(default=200, ge=1, le=1000)):
    return svc.audit(name, limit=limit)


@router.delete("/{name}/audit", summary="Clear the egress ledger")
def clear_audit(name: str, svc: ServiceDep) -> dict[str, Any]:
    svc.get_package(name)  # 404 if unknown
    return {"success": True, "removed": svc.clear_audit(name)}


@router.get("/{name}/health", summary="Stale items, open tasks, exposure, conflicts")
def package_health(
    name: str, svc: ServiceDep, stale_days: int = Query(default=30, ge=0, le=3650)
) -> dict[str, Any]:
    return svc.health(name, stale_days=stale_days)


# -- Portability -------------------------------------------------------------


@router.get("/{name}/export", summary="Download the package as a portable JSON file")
def export_package(name: str, svc: ServiceDep) -> Response:
    body = svc.export_package_json(name)
    filename = f"{validate_package_name(name)}.contextbridge.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{name}/embeddings", summary="Persisted embedding count (pgvector backends)")
def embeddings(name: str, svc: ServiceDep) -> dict[str, Any]:
    svc.get_package(name)
    count_fn = getattr(svc.store, "embedding_count", None)
    count = int(count_fn(name)) if callable(count_fn) else 0
    return {
        "package_name": name,
        "backend": svc.store.backend_name,
        "persistent": callable(count_fn),
        "embeddings": count,
    }


def dumps(obj: Any) -> str:  # pragma: no cover - helper for manual debugging
    return json.dumps(obj, indent=2, default=str)
