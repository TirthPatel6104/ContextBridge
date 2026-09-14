"""
ContextBridge Web Dashboard — local-first browser UI for context management.

The app is built by :func:`create_app` so tests can inject a temporary store.
All endpoints are JSON under ``/api/``; the single page at ``/`` drives the
import → review → retrieve → export workflow.

Run: ``python -m contextbridge.web.app`` (binds to 127.0.0.1:5000).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from contextbridge import __version__
from contextbridge.config import Settings
from contextbridge.core.file_parser import parse_file, parse_multiple_files
from contextbridge.core.redaction import describe_kinds
from contextbridge.models import MemoryCategory, RetrievalOptions
from contextbridge.service import ContextBridgeService
from contextbridge.storage import get_store
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.validation import (
    ValidationError,
    safe_filename,
    validate_package_name,
    validate_upload_filename,
)

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_options(payload: dict[str, Any]) -> RetrievalOptions:
    """Build RetrievalOptions from a JSON body, with friendly errors."""
    kwargs: dict[str, Any] = {}
    if payload.get("top_k") not in (None, ""):
        kwargs["top_k"] = int(payload["top_k"])
    if payload.get("token_budget") not in (None, ""):
        kwargs["token_budget"] = int(payload["token_budget"])
    if payload.get("min_score") not in (None, ""):
        kwargs["min_score"] = float(payload["min_score"])
    cats = payload.get("categories")
    if cats:
        if isinstance(cats, str):
            cats = [c for c in cats.split(",") if c.strip()]
        try:
            kwargs["categories"] = [MemoryCategory(c.strip()) for c in cats]
        except ValueError as exc:
            raise ValidationError(f"Unknown category: {exc}") from exc
    try:
        return RetrievalOptions(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid retrieval options: {exc}") from exc


def create_app(
    settings: Settings | None = None,
    *,
    store: StorageBackend | None = None,
    service: ContextBridgeService | None = None,
) -> Flask:
    """Application factory."""
    settings = settings or Settings.from_env()
    store = store or get_store(settings=settings)
    svc = service or ContextBridgeService(store, settings=settings)

    app = Flask(
        __name__,
        template_folder=str(_HERE / "templates"),
        static_folder=str(_HERE / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_bytes
    app.config["JSON_SORT_KEYS"] = False
    app.extensions["cb_service"] = svc
    app.extensions["cb_settings"] = settings

    # Only the chat sites (for the extension) may call the API cross-origin.
    CORS(app, resources={r"/api/*": {"origins": list(settings.allowed_origins)}})

    files_dir = settings.storage_dir / "attached_files"
    watch_dir = settings.storage_dir / "watch"

    def _package_files_dir(name: str, *, create: bool = False) -> Path:
        d = files_dir / validate_package_name(name)
        if create:
            d.mkdir(parents=True, exist_ok=True)
        return d

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
            body = fp.read_text(encoding="utf-8", errors="replace")
            sections.append((f"{title}: {fp.name}", body))
        return sections

    # -- Error handling ------------------------------------------------------

    @app.errorhandler(ValidationError)
    def _bad_request(exc: ValidationError):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(PackageNotFoundError)
    def _not_found(exc: PackageNotFoundError):
        return jsonify({"error": str(exc)}), 404

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(exc: RequestEntityTooLarge):
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        return jsonify({"error": f"Upload too large. The limit is {limit_mb} MB."}), 413

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        return jsonify({"error": exc.description or exc.name}), exc.code or 500

    @app.errorhandler(Exception)
    def _server_error(exc: Exception):
        # Never echo internals (which may include memory content) to the client.
        logger.exception("Unhandled error in %s", request.path)
        return jsonify({"error": "Internal error. Check the server log for details."}), 500

    # -- Pages ---------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template("index.html", version=__version__)

    # -- Meta ----------------------------------------------------------------

    @app.route("/api/health", methods=["GET"])
    def api_health():
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "storage_backend": store.backend_name,
                "storage_location": store.location(),
                "redact_by_default": settings.redact_by_default,
                "redaction_kinds": describe_kinds(),
                "categories": [c.value for c in MemoryCategory],
                "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
            }
        )

    @app.route("/api/local/models", methods=["GET"])
    def api_list_local_models():
        """List available local models from Ollama (empty list if it is not running)."""
        try:
            from contextbridge.adapters.local_adapter import LocalAdapter
            from contextbridge.service import _run

            models = _run(LocalAdapter.list_models())
            return jsonify({"models": models})
        except Exception:
            return jsonify({"models": [], "error": "Ollama unreachable"})

    # -- Import / extraction -------------------------------------------------

    @app.route("/api/extract", methods=["POST"])
    def api_extract():
        """
        Extract structured memory from uploaded files into a package.

        Multipart fields: ``files[]``, ``package_name``, ``model`` (engine),
        ``redact`` (default on), ``mode`` (append|replace).
        """
        package_name = validate_package_name(request.form.get("package_name"))
        engine = request.form.get("model") or "local"
        redact = _truthy(request.form.get("redact"), settings.redact_by_default)
        mode = (request.form.get("mode") or "append").strip().lower()

        uploads = [f for f in request.files.getlist("files[]") if f and f.filename]
        if not uploads:
            raise ValidationError("No files uploaded")

        file_data: list[tuple[bytes, str]] = []
        for f in uploads:
            name = validate_upload_filename(f.filename)
            file_data.append((f.read(), name))

        combined = parse_multiple_files(file_data)
        if not combined.strip():
            raise ValidationError("Could not extract any text from the uploaded files")

        origin = ", ".join(n for _, n in file_data)[:200]
        outcome = svc.import_text(
            combined,
            package_name,
            engine=engine,
            origin=origin,
            redact=redact,
            mode=mode,
        )
        return jsonify(
            {
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
                "warnings": outcome.warnings,
            }
        )

    @app.route("/api/redaction/scan", methods=["POST"])
    def api_redaction_scan():
        """Preview what the redactor would mask in a piece of text."""
        payload = request.get_json(silent=True) or {}
        text = payload.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("No text provided")
        report = svc.scan(text)
        return jsonify(
            {
                "summary": report.summary(),
                "findings": [f.model_dump() for f in report.findings[:200]],
            }
        )

    # -- Packages ------------------------------------------------------------

    @app.route("/api/packages", methods=["GET"])
    def api_list_packages():
        return jsonify({"packages": svc.list_packages()})

    @app.route("/api/packages/<name>", methods=["GET"])
    def api_get_package(name: str):
        pkg = svc.get_package(name)
        return jsonify(svc.package_to_dict(pkg))

    @app.route("/api/packages/<name>", methods=["DELETE"])
    def api_delete_package(name: str):
        svc.delete_package(name)
        return jsonify({"success": True, "deleted": name})

    @app.route("/api/packages/<name>/history", methods=["GET"])
    def api_history(name: str):
        return jsonify({"package_name": name, "history": svc.history(name)})

    @app.route("/api/packages/<name>/rollback", methods=["POST"])
    def api_rollback(name: str):
        payload = request.get_json(silent=True) or {}
        try:
            version = int(payload.get("version"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("A target version is required") from exc
        try:
            pkg = svc.rollback(name, version)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return jsonify({"success": True, "package_name": pkg.name, "version": pkg.version})

    @app.route("/api/packages/<name>/items/remove", methods=["POST"])
    def api_remove_items(name: str):
        payload = request.get_json(silent=True) or {}
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            raise ValidationError("'ids' must be a list")
        pkg = svc.remove_items(name, ids, note=str(payload.get("note") or ""))
        return jsonify(
            {
                "success": True,
                "package_name": pkg.name,
                "version": pkg.version,
                "total_items": pkg.memory.total_items,
                "memory": svc.memory_to_dict(pkg.memory),
            }
        )

    # -- Retrieval & prompt --------------------------------------------------

    @app.route("/api/retrieve", methods=["POST"])
    def api_retrieve():
        payload = request.get_json(silent=True) or {}
        name = validate_package_name(payload.get("package_name"))
        query = str(payload.get("query") or "")
        options = _parse_options(payload)
        result = svc.retrieve(name, query, options)
        return jsonify(
            {
                "package_name": name,
                "query": query,
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
                        "item": {
                            "id": s.item.id,
                            "category": s.item.category.value,
                            "content": s.item.content,
                            "confidence": s.item.confidence,
                            "source": s.item.source,
                            "origin": s.item.origin,
                            "redactions": [r.model_dump() for r in s.item.redactions],
                        },
                    }
                    for s in result.selected
                ],
            }
        )

    @app.route("/api/prompt", methods=["POST"])
    def api_prompt():
        """
        Generate a paste-ready prompt.

        JSON: ``package_name``, ``target_model``, optional ``query`` plus
        retrieval options, ``include_files`` (attached files, default off),
        ``include_watch`` (watch folder, default off).
        """
        payload = request.get_json(silent=True) or {}
        name = validate_package_name(payload.get("package_name"))
        target = str(payload.get("target_model") or "claude")
        query = payload.get("query")
        options = _parse_options(payload) if query else None

        extras: list[tuple[str, str]] = []
        attached: list[str] = []
        watched: list[str] = []
        if _truthy(payload.get("include_files")):
            d = _package_files_dir(name)
            extras.extend(_read_sections(d, "ATTACHED FILE"))
            attached = [e["name"] for e in _list_dir(d)]
        if _truthy(payload.get("include_watch")):
            extras.extend(_read_sections(watch_dir, "WATCH FOLDER FILE"))
            watched = [e["name"] for e in _list_dir(watch_dir)]

        built = svc.build_prompt(name, target, query=query, options=options, extras=extras)
        retrieval = built.pop("retrieval")
        built["retrieval"] = retrieval.summary() if retrieval else None
        built["attached_files"] = attached
        built["watched_files"] = watched
        built["success"] = True
        return jsonify(built)

    # -- Portability ---------------------------------------------------------

    @app.route("/api/packages/<name>/export", methods=["GET"])
    def api_export_package(name: str):
        body = svc.export_package_json(name)
        filename = f"{validate_package_name(name)}.contextbridge.json"
        return Response(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/api/packages/import", methods=["POST"])
    def api_import_package():
        rename = None
        overwrite = False
        if request.files.get("file"):
            upload = request.files["file"]
            safe_filename(upload.filename)
            data: bytes | dict = upload.read()
            rename = request.form.get("rename") or None
            overwrite = _truthy(request.form.get("overwrite"))
        else:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValidationError("Send a package file as 'file' or a JSON body")
            data = payload.get("package_file") or payload
            rename = payload.get("rename") or None
            overwrite = _truthy(payload.get("overwrite"))
        pkg = svc.import_package_data(data, rename=rename, overwrite=overwrite)
        return jsonify(
            {
                "success": True,
                "package_name": pkg.name,
                "version": pkg.version,
                "total_items": pkg.memory.total_items,
            }
        )

    @app.route("/api/packages/merge", methods=["POST"])
    def api_merge_packages():
        payload = request.get_json(silent=True) or {}
        names = payload.get("names") or []
        if not isinstance(names, list):
            raise ValidationError("'names' must be a list")
        new_name = payload.get("new_name") or ""
        dry_run = _truthy(payload.get("dry_run"))
        merged, result = svc.merge_packages(
            [str(n) for n in names],
            new_name,
            dry_run=dry_run,
            overwrite=_truthy(payload.get("overwrite")),
        )
        item = svc.memory_to_dict
        return jsonify(
            {
                "success": True,
                "dry_run": dry_run,
                "package_name": merged.name,
                "summary": result.summary(),
                "memory": item(result.memory),
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
        )

    # -- Evaluation ----------------------------------------------------------

    @app.route("/api/eval", methods=["GET"])
    def api_eval():
        from contextbridge.evaluation import run_evaluation

        report = run_evaluation()
        return jsonify(report.model_dump(mode="json"))

    # -- Attached files (explicit opt-in extras for prompts) -----------------

    @app.route("/api/files/attach", methods=["POST"])
    def api_attach_files():
        name = validate_package_name(request.form.get("package_name"))
        if not store.exists(name):
            raise PackageNotFoundError(f"Package '{name}' not found")
        uploads = [f for f in request.files.getlist("files[]") if f and f.filename]
        if not uploads:
            raise ValidationError("No files uploaded")
        target_dir = _package_files_dir(name, create=True)
        saved = []
        for f in uploads:
            original = safe_filename(f.filename)
            text = parse_file(f.read(), original)
            stored_as = Path(original).stem + ".txt"
            (target_dir / stored_as).write_text(text, encoding="utf-8")
            saved.append({"original": original, "stored_as": stored_as, "chars": len(text)})
        return jsonify({"success": True, "package_name": name, "files_attached": saved})

    @app.route("/api/files/<name>", methods=["GET"])
    def api_list_files(name: str):
        return jsonify({"package_name": name, "files": _list_dir(_package_files_dir(name))})

    @app.route("/api/watch/files", methods=["GET"])
    def api_list_watch_files():
        return jsonify({"files": _list_dir(watch_dir), "watch_dir": str(watch_dir)})

    return app


def _item_dict(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "category": item.category.value,
        "content": item.content,
        "confidence": item.confidence,
        "source": item.source,
        "origin": item.origin,
    }


def main() -> None:  # pragma: no cover - manual entry point
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=os.getenv("CB_LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    application = create_app(settings)
    print("\n  ContextBridge Dashboard")
    print(f"  http://{settings.host}:{settings.port}")
    print(f"  Storage: {application.extensions['cb_service'].store.location()}\n")
    application.run(host=settings.host, port=settings.port, debug=_truthy(os.getenv("CB_DEBUG")))


if __name__ == "__main__":  # pragma: no cover
    main()
