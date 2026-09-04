"""
ContextBridge Web Dashboard — browser-based UI for context management.

A sleek single-page web app for extracting, managing, and transferring
conversational context between LLMs. Supports drag-and-drop file upload
for ChatGPT exports, PDFs, documents, and chat transcripts.

Run: python -m contextbridge.web.app
"""

from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from contextbridge.adapters import get_adapter
from contextbridge.core.file_parser import parse_file, parse_multiple_files
from contextbridge.core.memory import MemoryExtractor
from contextbridge.core.packager import ContextPackager
from contextbridge.core.prompt_builder import PromptBuilder
from contextbridge.storage.json_store import JSONStore

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload
CORS(app)  # Allow extension & cross-origin requests

packager = ContextPackager()
prompt_builder = PromptBuilder()

# Directory to store attached files per package
# Directory to store attached files per package
FILES_DIR = Path.home() / ".contextbridge" / "attached_files"
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Watch directory for auto-upload
WATCH_DIR = Path.home() / ".contextbridge" / "watch"
WATCH_DIR.mkdir(parents=True, exist_ok=True)


def _get_store() -> JSONStore:
    return JSONStore()


def _get_files_dir(package_name: str) -> Path:
    d = FILES_DIR / package_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.route("/api/local/models", methods=["GET"])
def api_list_local_models():
    """List available local models from Ollama."""
    try:
        from contextbridge.adapters.local_adapter import LocalAdapter

        # We need an async loop for the async list_models method
        models = _run_async(LocalAdapter.list_models())
        return jsonify({"models": models})
    except Exception as e:
        # Return empty list if Ollama is down, rather than 500
        return jsonify({"models": [], "error": str(e)})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    """Extract structured memory from uploaded files.

    Accepts multipart/form-data with:
      - files[]: one or more files (ZIP, PDF, TXT, JSON, HTML, MD)
      - model: extraction engine - 'local' (default, no API key) or 'openai'/'claude'
      - package_name: name for the context package
    """
    try:
        model = request.form.get("model", "local")
        package_name = request.form.get("package_name", "").strip()

        if not package_name:
            return jsonify({"error": "No package name provided"}), 400

        # Collect all uploaded files
        uploaded_files = request.files.getlist("files[]")
        if not uploaded_files or all(f.filename == "" for f in uploaded_files):
            return jsonify({"error": "No files uploaded"}), 400

        # Parse all files into combined text
        file_data = []
        file_names = []
        for f in uploaded_files:
            if f.filename:
                file_bytes = f.read()
                file_data.append((file_bytes, f.filename))
                file_names.append(f.filename)

        if not file_data:
            return jsonify({"error": "No valid files uploaded"}), 400

        combined_text = parse_multiple_files(file_data)

        if not combined_text.strip():
            return jsonify({"error": "Could not extract any text from the uploaded files"}), 400

        # Extract structured memory
        if model == "local":
            # Local extraction — no API key needed
            from contextbridge.core.local_extractor import LocalExtractor

            local_ext = LocalExtractor()
            memory = local_ext.extract(combined_text)
        else:
            # API-based extraction (needs API key)
            adapter = get_adapter(model)
            extractor = MemoryExtractor()
            memory = _run_async(extractor.extract(combined_text, adapter, source_model=model))

        # Package and save
        store = _get_store()
        if store.exists(package_name):
            existing = store.load(package_name)
            pkg = packager.update(existing, memory, source_model=model)
        else:
            pkg = packager.create(package_name, memory, source_model=model)

        store.save(pkg)

        # Serialize memory for response
        from contextbridge.models import MemoryCategory

        memory_data = {}
        for cat in MemoryCategory:
            items = memory.get_category(cat)
            memory_data[cat.value] = [
                {"content": item.content, "confidence": item.confidence, "source": item.source}
                for item in items
            ]

        return jsonify(
            {
                "success": True,
                "package_name": package_name,
                "version": pkg.version,
                "total_items": memory.total_items,
                "memory": memory_data,
                "files_processed": file_names,
                "chars_extracted": len(combined_text),
            }
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/prompt", methods=["POST"])
def api_prompt():
    """Generate a paste-ready prompt for a target model.

    Includes structured memory context AND full content of any attached files.
    """
    try:
        data = request.get_json()
        package_name = data.get("package_name", "").strip()
        target_model = data.get("target_model", "claude")

        if not package_name:
            return jsonify({"error": "No package name provided"}), 400

        store = _get_store()
        if not store.exists(package_name):
            return jsonify({"error": f"Package '{package_name}' not found"}), 404

        pkg = store.load(package_name)
        context_block = prompt_builder.build(pkg, target_model)

        target_name = "Claude" if "claude" in target_model.lower() else "ChatGPT"

        # Build the prompt with context
        parts = [
            "I'm continuing a conversation from another AI assistant. "
            "Below is structured context extracted from our previous chats, "
            "along with the full content of files we were working with. "
            "Please internalise all of this as established knowledge and "
            "maintain full continuity.",
            "",
            context_block,
        ]

        # Embed attached file content
        files_dir = _get_files_dir(package_name)
        attached_files = []
        for fp in sorted(files_dir.iterdir()):
            if fp.is_file() and not fp.name.startswith("."):
                attached_files.append(fp)

        if attached_files:
            parts.append("")
            parts.append("=" * 60)
            parts.append("ATTACHED FILES (full content from original conversation)")
            parts.append("=" * 60)
            for fp in attached_files:
                content = fp.read_text(encoding="utf-8", errors="replace")
                parts.append(f"\n--- FILE: {fp.name} ---")
                parts.append(content)
                parts.append(f"--- END: {fp.name} ---")

        # Embed watched files (auto-detected)
        watched_files = []
        if WATCH_DIR.exists():
            for fp in sorted(WATCH_DIR.iterdir()):
                if fp.is_file() and not fp.name.startswith("."):
                    watched_files.append(fp)

        if watched_files:
            parts.append("")
            parts.append("=" * 60)
            parts.append("AUTO-DETECTED FILES (from watch folder)")
            parts.append("=" * 60)
            for fp in watched_files:
                try:
                    # Basic text check
                    content = fp.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"\n--- FILE: {fp.name} ---")
                    parts.append(content)
                    parts.append(f"--- END: {fp.name} ---")
                except Exception:
                    parts.append(f"\n--- FILE: {fp.name} (Binary/Unreadable) ---")

        parts.append("")
        parts.append("---")
        parts.append(
            "Please confirm you've understood this context and the attached files, "
            "then I'll continue with my questions."
        )

        paste_prompt = "\n".join(parts)

        return jsonify(
            {
                "success": True,
                "prompt": paste_prompt,
                "target_model": target_name,
                "package_name": package_name,
                "version": pkg.version,
                "total_items": pkg.memory.total_items,
                "attached_files": [f.name for f in attached_files],
                "watched_files": [f.name for f in watched_files],
            }
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/files/attach", methods=["POST"])
def api_attach_files():
    """Attach files to a package. Parses their content and stores it.

    Accepts multipart/form-data with:
      - files[]: one or more files
      - package_name: which package to attach to
    """
    try:
        package_name = request.form.get("package_name", "").strip()
        if not package_name:
            return jsonify({"error": "No package name provided"}), 400

        uploaded = request.files.getlist("files[]")
        if not uploaded or all(f.filename == "" for f in uploaded):
            return jsonify({"error": "No files uploaded"}), 400

        files_dir = _get_files_dir(package_name)
        saved = []

        for f in uploaded:
            if not f.filename:
                continue

            raw_bytes = f.read()
            filename = f.filename

            # Parse file to text
            text = parse_file(raw_bytes, filename)

            # Save the extracted text
            safe_name = Path(filename).stem + ".txt"
            dest = files_dir / safe_name
            dest.write_text(text, encoding="utf-8")
            saved.append({"original": filename, "stored_as": safe_name, "chars": len(text)})

        return jsonify(
            {
                "success": True,
                "package_name": package_name,
                "files_attached": saved,
            }
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/files/<name>", methods=["GET"])
def api_list_files(name: str):
    """List files attached to a package."""
    try:
        files_dir = _get_files_dir(name)
        files = []
        for fp in sorted(files_dir.iterdir()):
            if fp.is_file() and not fp.name.startswith("."):
                files.append(
                    {
                        "name": fp.name,
                        "size": fp.stat().st_size,
                    }
                )
        return jsonify({"package_name": name, "files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watch/files", methods=["GET"])
def api_list_watch_files():
    """List files currently in the watch folder."""
    try:
        files = []
        if WATCH_DIR.exists():
            for fp in sorted(WATCH_DIR.iterdir()):
                if fp.is_file() and not fp.name.startswith("."):
                    files.append(
                        {
                            "name": fp.name,
                            "size": fp.stat().st_size,
                        }
                    )
        return jsonify({"files": files, "watch_dir": str(WATCH_DIR)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/packages", methods=["GET"])
def api_list_packages():
    """List all stored context packages."""
    try:
        store = _get_store()
        packages = store.list_packages()

        result = []
        for name in packages:
            pkg = store.load(name)
            result.append(
                {
                    "name": name,
                    "version": pkg.version,
                    "source_model": pkg.source_model or "unknown",
                    "total_items": pkg.memory.total_items,
                    "updated_at": pkg.updated_at.strftime("%Y-%m-%d %H:%M"),
                }
            )

        return jsonify({"packages": result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/packages/<name>", methods=["GET"])
def api_get_package(name: str):
    """Get details of a specific package."""
    try:
        store = _get_store()
        if not store.exists(name):
            return jsonify({"error": f"Package '{name}' not found"}), 404

        pkg = store.load(name)

        from contextbridge.models import MemoryCategory

        memory_data = {}
        for cat in MemoryCategory:
            items = pkg.memory.get_category(cat)
            memory_data[cat.value] = [
                {"content": item.content, "confidence": item.confidence, "source": item.source}
                for item in items
            ]

        return jsonify(
            {
                "name": pkg.name,
                "version": pkg.version,
                "source_model": pkg.source_model,
                "total_items": pkg.memory.total_items,
                "created_at": pkg.created_at.strftime("%Y-%m-%d %H:%M"),
                "updated_at": pkg.updated_at.strftime("%Y-%m-%d %H:%M"),
                "memory": memory_data,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/packages/<name>", methods=["DELETE"])
def api_delete_package(name: str):
    """Delete a context package."""
    try:
        store = _get_store()
        if not store.exists(name):
            return jsonify({"error": f"Package '{name}' not found"}), 404

        store.delete(name)
        return jsonify({"success": True, "deleted": name})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    print("\n  ContextBridge Dashboard")
    print("  http://localhost:5000\n")
    app.run(debug=True, port=5000)
