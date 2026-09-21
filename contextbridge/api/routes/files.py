"""Attached files: explicit opt-in extras for prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from contextbridge.api.deps import ServiceDep, SettingsDep
from contextbridge.core.file_parser import parse_file
from contextbridge.storage.base import PackageNotFoundError
from contextbridge.validation import ValidationError, safe_filename, validate_package_name

router = APIRouter(tags=["files"])


def _list_dir(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [
        {"name": fp.name, "size": fp.stat().st_size}
        for fp in sorted(directory.iterdir())
        if fp.is_file() and not fp.name.startswith(".")
    ]


@router.post("/files/attach", summary="Attach files to a package (included only on request)")
async def attach(request: Request, svc: ServiceDep, settings: SettingsDep) -> dict[str, Any]:
    form = await request.form()
    name = validate_package_name(form.get("package_name"))
    if not svc.store.exists(name):
        raise PackageNotFoundError(f"Package '{name}' not found")
    uploads = [
        f
        for f in list(form.getlist("files[]")) + list(form.getlist("files"))
        if getattr(f, "filename", None)
    ]
    if not uploads:
        raise ValidationError("No files uploaded")
    target_dir = settings.storage_dir / "attached_files" / name
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for upload in uploads:
        original = safe_filename(upload.filename)
        text = parse_file(await upload.read(), original)
        stored_as = Path(original).stem + ".txt"
        (target_dir / stored_as).write_text(text, encoding="utf-8")
        saved.append({"original": original, "stored_as": stored_as, "chars": len(text)})
    return {"success": True, "package_name": name, "files_attached": saved}


@router.get("/files/{name}", summary="List files attached to a package")
def list_files(name: str, settings: SettingsDep) -> dict[str, Any]:
    directory = settings.storage_dir / "attached_files" / validate_package_name(name)
    return {"package_name": name, "files": _list_dir(directory)}


@router.get("/watch/files", summary="List files in the watch folder")
def watch_files(settings: SettingsDep) -> dict[str, Any]:
    watch_dir = settings.storage_dir / "watch"
    return {"files": _list_dir(watch_dir), "watch_dir": str(watch_dir)}
