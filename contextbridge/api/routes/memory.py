"""The workflow endpoints: extract → scan → retrieve → prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from contextbridge.api.deps import ServiceDep, SettingsDep, parse_options, truthy
from contextbridge.api.schemas import PromptRequest, RetrieveRequest, ScanRequest
from contextbridge.core.file_parser import parse_multiple_files
from contextbridge.validation import (
    ValidationError,
    validate_package_name,
    validate_upload_filename,
)

router = APIRouter(tags=["memory"])


def _uploads(form: Any) -> list[Any]:
    files = list(form.getlist("files[]")) + list(form.getlist("files"))
    return [f for f in files if getattr(f, "filename", None)]


@router.post("/extract", summary="Extract memory from uploaded transcripts into a package")
async def extract(request: Request, svc: ServiceDep, settings: SettingsDep) -> dict[str, Any]:
    """
    Multipart fields: ``files[]`` (one or more), ``package_name``, ``model``
    (engine: local | openai | claude | an Ollama model), ``redact`` (default
    on), ``mode`` (append | replace).
    """
    form = await request.form()
    package_name = validate_package_name(form.get("package_name"))
    engine = str(form.get("model") or "local")
    redact = truthy(form.get("redact"), settings.redact_by_default)
    mode = str(form.get("mode") or "append").strip().lower()

    uploads = _uploads(form)
    if not uploads:
        raise ValidationError("No files uploaded")

    file_data: list[tuple[bytes, str]] = []
    for upload in uploads:
        name = validate_upload_filename(upload.filename)
        file_data.append((await upload.read(), name))

    combined = parse_multiple_files(file_data)
    if not combined.strip():
        raise ValidationError("Could not extract any text from the uploaded files")

    origin = ", ".join(n for _, n in file_data)[:200]
    outcome = svc.import_text(
        combined, package_name, engine=engine, origin=origin, redact=redact, mode=mode
    )
    return {
        "success": True,
        "package_name": outcome.package.name,
        "version": outcome.package.version,
        "created": outcome.created,
        "engine": outcome.engine,
        "total_items": outcome.package.memory.total_items,
        "added_items": outcome.added_items,
        "extracted_items": outcome.extracted.total_items,
        "memory": svc.memory_to_dict(outcome.extracted),
        "redaction": outcome.redaction.summary(),
        "files_processed": [n for _, n in file_data],
        "chars_extracted": outcome.chars,
        "tokens_transcript": outcome.transcript_tokens,
        "re_seen_items": outcome.re_seen_items,
        "warnings": outcome.warnings,
        "notes": outcome.notes,
        "handoff": outcome.handoff.model_dump(mode="json") if outcome.handoff else None,
        "conflicts": [c.model_dump(mode="json") for c in outcome.conflicts],
    }


@router.post("/redaction/scan", summary="Preview what the redactor would mask")
def scan(body: ScanRequest, svc: ServiceDep) -> dict[str, Any]:
    if not body.text.strip():
        raise ValidationError("No text provided")
    report = svc.scan(body.text)
    return {
        "summary": report.summary(),
        "findings": [f.model_dump() for f in report.findings[:200]],
    }


@router.post("/retrieve", summary="Explainable retrieval over a package")
def retrieve(body: RetrieveRequest, svc: ServiceDep) -> dict[str, Any]:
    name = validate_package_name(body.package_name)
    options = parse_options(body.model_dump())
    result = svc.retrieve(name, body.query, options)
    return {
        "package_name": name,
        "query": body.query,
        "summary": result.summary(),
        "options": result.options.model_dump(),
        "selected": [
            {
                "rank": s.rank,
                "score": s.score,
                "lexical_score": s.lexical_score,
                "embedding_score": s.embedding_score,
                "matched_terms": s.matched_terms,
                "reason": s.reason,
                "tokens": s.tokens,
                "item": svc.item_to_dict(s.item),
            }
            for s in result.selected
        ],
    }


def _list_dir(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [
        {"name": fp.name, "size": fp.stat().st_size}
        for fp in sorted(directory.iterdir())
        if fp.is_file() and not fp.name.startswith(".")
    ]


def _read_sections(directory: Path, title: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for entry in _list_dir(directory):
        fp = directory / entry["name"]
        sections.append((f"{title}: {fp.name}", fp.read_text(encoding="utf-8", errors="replace")))
    return sections


@router.post("/prompt", summary="Build a paste-ready prompt for a target model")
def prompt(body: PromptRequest, svc: ServiceDep, settings: SettingsDep) -> dict[str, Any]:
    """
    The package is partitioned by the target's sharing boundary, optionally
    narrowed by retrieval (``query`` plus retrieval options), rendered in the
    target's preferred format, and — unless ``record`` is false — written to
    the egress ledger.
    """
    name = validate_package_name(body.package_name)
    options = parse_options(body.model_dump()) if body.query else None

    extras: list[tuple[str, str]] = []
    attached: list[str] = []
    watched: list[str] = []
    files_dir = settings.storage_dir / "attached_files" / name
    watch_dir = settings.storage_dir / "watch"
    if body.include_files:
        extras.extend(_read_sections(files_dir, "ATTACHED FILE"))
        attached = [e["name"] for e in _list_dir(files_dir)]
    if body.include_watch:
        extras.extend(_read_sections(watch_dir, "WATCH FOLDER FILE"))
        watched = [e["name"] for e in _list_dir(watch_dir)]

    built = svc.build_prompt(
        name,
        body.target_model,
        query=body.query,
        options=options,
        extras=extras,
        surface=body.surface,
        record=body.record,
    )
    retrieval = built.pop("retrieval")
    built["retrieval"] = retrieval.summary() if retrieval else None
    built["withheld"] = svc.withheld_to_dicts(built.pop("withheld"))
    built["attached_files"] = attached
    built["watched_files"] = watched
    built["recorded"] = body.record
    built["success"] = True
    return built
