"""
ContextBridge CLI — command-line interface for portable AI working memory.

Workflow commands
    cb import        Extract memory from transcripts / exports into a package
    cb retrieve      Explainable retrieval: scores, matched terms, token savings
    cb prompt        Paste-ready prompt for ChatGPT / Claude / a local model
    cb query         Ask a model a question with relevant memory injected

Package commands
    cb list / inspect / history / rollback / remove / delete
    cb export-package / import-package     Portable JSON files
    cb merge                               Combine packages with review report

Privacy & quality
    cb scan          Show what the redactor would mask in a file
    cb eval          Run the offline evaluation suite
    cb info          Storage backend, location, and stats
    cb migrate       Import legacy JSON packages into SQLite

Legacy aliases kept for compatibility: ``cb export`` and ``cb web-export``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from contextbridge import __version__
from contextbridge.config import Settings
from contextbridge.core.file_parser import parse_multiple_files
from contextbridge.core.redaction import DETECTOR_LABELS
from contextbridge.models import (
    CATEGORY_LABELS,
    MemoryCategory,
    RetrievalOptions,
    RetrievalResult,
    StructuredMemory,
)
from contextbridge.service import ContextBridgeService
from contextbridge.storage import get_store, import_legacy_json
from contextbridge.storage.base import PackageNotFoundError
from contextbridge.storage.json_store import JSONStore
from contextbridge.storage.sqlite_store import SQLiteStore
from contextbridge.validation import ValidationError

console = Console()

ENGINE_HELP = (
    "Extraction engine: 'local' (offline rules, default), 'openai', 'claude', "
    "or an Ollama model name such as 'llama3'."
)
TARGET_CHOICES = click.Choice(["openai", "claude", "local", "ollama"], case_sensitive=False)
CATEGORY_CHOICES = click.Choice([c.value for c in MemoryCategory], case_sensitive=False)

_CATEGORY_ICONS = {
    MemoryCategory.IDENTITY: "👤",
    MemoryCategory.PROJECTS: "📁",
    MemoryCategory.FACTS: "📌",
    MemoryCategory.DECISIONS: "⚖️",
    MemoryCategory.OPEN_TASKS: "📋",
    MemoryCategory.PREFERENCES: "⚙️",
}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class Context:
    """Per-invocation wiring (no module-level mutable state)."""

    def __init__(self, storage_dir: str | None, backend: str | None) -> None:
        overrides = {}
        if storage_dir:
            overrides["storage_dir"] = Path(storage_dir).expanduser()
        if backend:
            overrides["storage_backend"] = backend
        self.settings = Settings.from_env(**overrides)
        self._service: ContextBridgeService | None = None

    @property
    def service(self) -> ContextBridgeService:
        if self._service is None:
            store = get_store(settings=self.settings)
            self._service = ContextBridgeService(store, settings=self.settings)
        return self._service


pass_ctx = click.make_pass_decorator(Context)


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]✗[/] {message}")
    sys.exit(code)


def _handle_errors(func):
    """Turn service errors into friendly CLI failures."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PackageNotFoundError as exc:
            _fail(str(exc))
        except ValidationError as exc:
            _fail(str(exc), code=2)

    return wrapper


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy on Windows / macOS / Linux."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        else:
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                    break
            else:
                return False
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _read_inputs(paths: tuple[Path, ...]) -> tuple[str, str]:
    """Parse one or more files into combined text; returns (text, origin)."""
    data = [(p.read_bytes(), p.name) for p in paths]
    text = parse_multiple_files(data)
    origin = ", ".join(p.name for p in paths)[:200]
    return text, origin


# ---------------------------------------------------------------------------
# Main group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="ContextBridge")
@click.option(
    "--storage-dir",
    envvar="CB_STORAGE_DIR",
    default=None,
    help="Storage directory (default ~/.contextbridge).",
)
@click.option(
    "--backend",
    type=click.Choice(["sqlite", "json"], case_sensitive=False),
    envvar="CB_STORAGE_BACKEND",
    default=None,
    help="Storage backend (default sqlite).",
)
@click.pass_context
def main(ctx: click.Context, storage_dir: str | None, backend: str | None):
    """
    ContextBridge — portable, privacy-conscious working memory for AI tools.

    Extract structured memory from chats, review and redact it, retrieve only
    what matters, and paste it into ChatGPT, Claude, or a local model.
    """
    _make_streams_tolerant()
    ctx.obj = Context(storage_dir, backend)


def _make_streams_tolerant() -> None:
    """Never crash on emoji / box characters when stdout uses a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - closed or exotic streams
            pass


# ---------------------------------------------------------------------------
# Import / extraction
# ---------------------------------------------------------------------------


@main.command(name="import")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--output", "-o", "package", required=True, help="Package name to create/update.")
@click.option("--engine", "-e", default="local", show_default=True, help=ENGINE_HELP)
@click.option(
    "--redact/--no-redact",
    default=None,
    help="Mask secrets/PII before storing (default: on, see CB_REDACT).",
)
@click.option(
    "--mode",
    type=click.Choice(["append", "replace"]),
    default="append",
    show_default=True,
    help="Append to an existing package or replace its memory.",
)
@pass_ctx
@_handle_errors
def import_cmd(ctx: Context, files: tuple[Path, ...], package: str, engine: str, redact, mode):
    """Extract memory from transcripts, ChatGPT exports, PDFs, or Markdown."""
    text, origin = _read_inputs(files)
    if not text.strip():
        _fail("No text could be read from the given files")

    with console.status(f"[bold green]Extracting memory with '{engine}'..."):
        outcome = ctx.service.import_text(
            text, package, engine=engine, origin=origin, redact=redact, mode=mode
        )

    _display_memory(outcome.extracted, title="Extracted this run")
    _display_redaction(outcome.redaction)
    for warning in outcome.warnings:
        console.print(f"[yellow]⚠[/] {warning}")

    verb = "Created" if outcome.created else "Updated"
    console.print(
        f"\n[green]✓[/] {verb} package [bold]{outcome.package.name}[/] "
        f"→ v{outcome.package.version} "
        f"({outcome.added_items} new, {outcome.package.memory.total_items} total items)"
    )
    console.print(
        f"[dim]Transcript ≈ {outcome.transcript_tokens:,} tokens · "
        f"stored in {ctx.service.store.location()}[/]"
    )


@main.command()
@click.option("--model", "-m", required=True, help=ENGINE_HELP)
@click.option(
    "--chat", "-c", required=True, type=click.Path(exists=True, path_type=Path), help="Transcript."
)
@click.option("--output", "-o", required=True, help="Name for the context package.")
@click.option("--redact/--no-redact", default=None, help="Mask secrets/PII before storing.")
@pass_ctx
@_handle_errors
def export(ctx: Context, model: str, chat: Path, output: str, redact):
    """(Legacy alias) Extract memory from one transcript — same as ``cb import``."""
    text, origin = _read_inputs((chat,))
    with console.status("[bold green]Extracting structured memory..."):
        outcome = ctx.service.import_text(text, output, engine=model, origin=origin, redact=redact)
    _display_memory(outcome.extracted)
    _display_redaction(outcome.redaction)
    verb = "Created" if outcome.created else "Updated"
    console.print(f"\n[green]✓[/] {verb} package → v{outcome.package.version}")


@main.command(name="web-export")
@click.option("--model", "-m", default="local", show_default=True, help=ENGINE_HELP)
@click.option("--output", "-o", required=True, help="Name for the context package.")
@click.option(
    "--chat",
    "-c",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Chat file path. If omitted, reads from stdin (paste mode).",
)
@click.option(
    "--target",
    "-t",
    type=TARGET_CHOICES,
    default=None,
    help="Immediately print a paste-ready prompt for this target model.",
)
@click.option("--redact/--no-redact", default=None, help="Mask secrets/PII before storing.")
@pass_ctx
@_handle_errors
def web_export(ctx: Context, model: str, output: str, chat: Path | None, target, redact):
    """Extract from a pasted web chat (stdin) and optionally emit a prompt."""
    if chat:
        raw_text, origin = _read_inputs((chat,))
    else:
        console.print(
            Panel(
                "[bold cyan]Paste your web chat below[/]\n"
                "[dim]Then press Ctrl+Z (Windows) or Ctrl+D (macOS/Linux) followed by Enter.[/]",
                title="ContextBridge Web Export",
                border_style="cyan",
            )
        )
        raw_text = sys.stdin.read()
        origin = "pasted chat"

    if not raw_text.strip():
        _fail("No chat content provided")

    with console.status("[bold green]Extracting structured memory..."):
        outcome = ctx.service.import_text(
            raw_text, output, engine=model, origin=origin, redact=redact
        )
    _display_memory(outcome.extracted)
    _display_redaction(outcome.redaction)
    verb = "Created" if outcome.created else "Updated"
    console.print(f"\n[green]✓[/] {verb} package → v{outcome.package.version}")

    if target:
        built = ctx.service.build_prompt(output, target)
        console.print(
            Panel(
                built["prompt"],
                title=f"Paste this into {built['target_name']} ↓",
                border_style="green",
                padding=(1, 2),
            )
        )
        if _copy_to_clipboard(built["prompt"]):
            console.print("[green]✓[/] Also copied to clipboard")
    else:
        console.print(
            f"\n[dim]Next: [bold]cb prompt {output} --model claude --copy[/bold] "
            f"for a paste-ready prompt.[/]"
        )


# ---------------------------------------------------------------------------
# Retrieval & prompts
# ---------------------------------------------------------------------------


def _options_from_flags(top_k, categories, budget, min_score) -> RetrievalOptions:
    kwargs = {"top_k": top_k, "min_score": min_score}
    if categories:
        kwargs["categories"] = [MemoryCategory(c.lower()) for c in categories]
    if budget:
        kwargs["token_budget"] = budget
    return RetrievalOptions(**kwargs)


@main.command()
@click.argument("name")
@click.argument("query")
@click.option("--top-k", "-k", default=5, show_default=True, type=click.IntRange(1, 100))
@click.option(
    "--category",
    "-c",
    "categories",
    multiple=True,
    type=CATEGORY_CHOICES,
    help="Restrict to categories (repeatable).",
)
@click.option("--budget", "-b", type=click.IntRange(1), default=None, help="Token budget.")
@click.option("--min-score", type=click.FloatRange(0, 1), default=0.0, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print the full result as JSON.")
@pass_ctx
@_handle_errors
def retrieve(ctx: Context, name, query, top_k, categories, budget, min_score, as_json):
    """Show which memory items a query would select, and why."""
    options = _options_from_flags(top_k, categories, budget, min_score)
    result = ctx.service.retrieve(name, query, options)
    if as_json:
        click.echo(result.model_dump_json(indent=2))
        return
    _display_retrieval(result, name)


@main.command()
@click.argument("name")
@click.option("--model", "-m", required=True, type=TARGET_CHOICES, help="Target model.")
@click.option("--query", "-q", default=None, help="Only include memory relevant to this query.")
@click.option("--top-k", "-k", default=5, show_default=True, type=click.IntRange(1, 100))
@click.option("--category", "-c", "categories", multiple=True, type=CATEGORY_CHOICES)
@click.option("--budget", "-b", type=click.IntRange(1), default=None, help="Token budget.")
@click.option("--copy", is_flag=True, default=False, help="Copy the prompt to the clipboard.")
@click.option("--raw", is_flag=True, default=False, help="Print only the prompt (for piping).")
@pass_ctx
@_handle_errors
def prompt(ctx: Context, name, model, query, top_k, categories, budget, copy, raw):
    """Generate a paste-ready context prompt for a web UI or local model.

    \b
    Examples:
      cb prompt project --model claude --copy
      cb prompt project --model openai --query "database schema" --top-k 4
    """
    options = _options_from_flags(top_k, categories, budget, 0.0) if query else None
    built = ctx.service.build_prompt(name, model, query=query, options=options)
    text = built["prompt"]

    if raw:
        click.echo(text)
    else:
        retrieval: RetrievalResult | None = built["retrieval"]
        meta = (
            f"[dim]Package: {name} v{built['version']} · "
            f"{built['included_items']} of {built['total_items']} items · "
            f"≈ {built['tokens_prompt']:,} tokens"
        )
        if retrieval:
            saved = built["tokens_full_prompt"] - built["tokens_prompt"]
            meta += f" (≈ {max(saved, 0):,} fewer than the full package)"
        console.print(
            Panel(
                f"[bold green]Paste-ready prompt for {built['target_name']}[/]\n{meta}[/]",
                title="ContextBridge → Web UI",
                border_style="green",
            )
        )
        console.print(Panel(text, border_style="dim", padding=(1, 2)))

    if copy:
        if _copy_to_clipboard(text):
            console.print("\n[green]✓[/] Copied to clipboard.")
        else:
            console.print("\n[yellow]⚠[/] Could not access the clipboard; copy the text above.")
    elif not raw:
        console.print("\n[dim]Tip: --copy puts it on the clipboard, --raw pipes it to a file.[/]")


@main.command()
@click.argument("question")
@click.option("--context", "-c", required=True, help="Name of the context package to use.")
@click.option(
    "--model", "-m", required=True, type=TARGET_CHOICES, help="Adapter that answers the question."
)
@click.option("--smart/--no-smart", default=True, help="Inject only retrieval-selected memory.")
@click.option("--top-k", "-k", default=5, show_default=True, type=click.IntRange(1, 100))
@pass_ctx
@_handle_errors
def query(ctx: Context, question: str, context: str, model: str, smart: bool, top_k: int):
    """Ask a question with relevant context injected (calls the model API)."""
    from contextbridge.adapters import get_adapter

    pkg = ctx.service.get_package(context)
    adapter_name = "local" if model.lower() in {"local", "ollama"} else model
    adapter = get_adapter(adapter_name)

    if smart and pkg.memory.total_items > 0:
        result = ctx.service.retrieve_from_memory(
            pkg.memory, question, RetrievalOptions(top_k=top_k), adapter=adapter
        )
        system_prompt = ctx.service.builder.build_from_retrieval(pkg, result, model)
        console.print(
            f"[dim]Injecting {len(result.selected)} of {pkg.memory.total_items} items "
            f"({result.method} retrieval, ≈ {result.tokens_selected:,} tokens).[/]"
        )
    else:
        system_prompt = ctx.service.builder.build(pkg, model)

    console.print(
        Panel(
            f"[bold]Query:[/] {question}\n"
            f"[dim]Context: {context} v{pkg.version} · Model: {model}[/]",
            title="ContextBridge Query",
            border_style="magenta",
        )
    )
    from contextbridge.service import _run

    with console.status("[bold green]Querying model..."):
        response = _run(adapter.send(question, system_prompt=system_prompt))
    console.print(Panel(response, title="Response", border_style="green"))


# ---------------------------------------------------------------------------
# Package management
# ---------------------------------------------------------------------------


@main.command(name="list")
@pass_ctx
def list_packages(ctx: Context):
    """List all stored context packages."""
    summaries = ctx.service.list_packages()
    if not summaries:
        console.print(
            "[dim]No context packages yet. Import one with "
            "[bold]cb import chat.txt -o my_project[/bold].[/]"
        )
        return
    table = Table(title="Stored context packages", border_style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Version", justify="center")
    table.add_column("Source", style="yellow")
    table.add_column("Items", justify="center")
    table.add_column("Updated", style="dim")
    for s in summaries:
        table.add_row(
            s["name"],
            f"v{s['version']}",
            s["source_model"] or "—",
            str(s["total_items"]),
            str(s["updated_at"])[:16].replace("T", " "),
        )
    console.print(table)
    console.print(f"[dim]{ctx.service.store.backend_name} · {ctx.service.store.location()}[/]")


@main.command()
@click.argument("name")
@click.option("--ids", is_flag=True, help="Show item ids (for cb remove).")
@pass_ctx
@_handle_errors
def inspect(ctx: Context, name: str, ids: bool):
    """Inspect the contents of a context package."""
    pkg = ctx.service.get_package(name)
    redacted = sum(1 for i in pkg.memory.all_items if i.was_redacted)
    console.print(
        Panel(
            f"[bold]{pkg.name}[/] v{pkg.version} (schema v{pkg.schema_version})\n"
            f"Source: [yellow]{pkg.source_model or 'unknown'}[/]\n"
            f"Created: [dim]{pkg.created_at:%Y-%m-%d %H:%M}[/]  "
            f"Updated: [dim]{pkg.updated_at:%Y-%m-%d %H:%M}[/]\n"
            f"Items: [cyan]{pkg.memory.total_items}[/]  Redacted items: [cyan]{redacted}[/]",
            title="Package",
            border_style="cyan",
        )
    )
    _display_memory(pkg.memory, show_ids=ids)


@main.command()
@click.argument("name")
@pass_ctx
@_handle_errors
def history(ctx: Context, name: str):
    """View version history of a context package."""
    pkg = ctx.service.get_package(name)
    table = Table(title=f"Version history — {name}", border_style="cyan")
    table.add_column("Version", style="bold", justify="center")
    table.add_column("Timestamp", style="dim")
    table.add_column("Source", style="yellow")
    table.add_column("Note")
    table.add_column("Added", style="green", justify="center")
    table.add_column("Removed", style="red", justify="center")
    for v in ctx.service.packager.get_version_summary(pkg):
        table.add_row(
            f"v{v['version']}",
            v["timestamp"][:19].replace("T", " "),
            v["source_model"] or "—",
            v["note"] or "—",
            str(v["added"]),
            str(v["removed"]),
        )
    console.print(table)
    console.print(f"\n[dim]Current: v{pkg.version} · {pkg.memory.total_items} items[/]")


@main.command()
@click.argument("name")
@click.option("--version", "-v", required=True, type=int, help="Target version.")
@pass_ctx
@_handle_errors
def rollback(ctx: Context, name: str, version: int):
    """Roll back a context package to a previous version."""
    old = ctx.service.get_package(name).version
    try:
        pkg = ctx.service.rollback(name, version)
    except ValueError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/] Rolled back '{name}' from v{old} → v{pkg.version}")


@main.command()
@click.argument("name")
@click.argument("item_ids", nargs=-1, required=True)
@pass_ctx
@_handle_errors
def remove(ctx: Context, name: str, item_ids: tuple[str, ...]):
    """Remove memory items by id (see ``cb inspect --ids``) as a new version."""
    before = ctx.service.get_package(name).memory.total_items
    pkg = ctx.service.remove_items(name, item_ids, note="removed via cli")
    console.print(f"[green]✓[/] Removed {before - pkg.memory.total_items} item(s) → v{pkg.version}")


@main.command()
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to delete this package?")
@pass_ctx
@_handle_errors
def delete(ctx: Context, name: str):
    """Delete a stored context package."""
    ctx.service.delete_package(name)
    console.print(f"[green]✓[/] Deleted package '{name}'")


# ---------------------------------------------------------------------------
# Portability & merging
# ---------------------------------------------------------------------------


@main.command(name="export-package")
@click.argument("name")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="File to write (default: <name>.contextbridge.json in the current directory).",
)
@pass_ctx
@_handle_errors
def export_package(ctx: Context, name: str, output: Path | None):
    """Write a package to a portable JSON file."""
    body = ctx.service.export_package_json(name)
    target = output or Path(f"{name}.contextbridge.json")
    target.write_text(body, encoding="utf-8")
    console.print(f"[green]✓[/] Exported '{name}' → {target}")


@main.command(name="import-package")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--name", "-n", default=None, help="Store under a different package name.")
@click.option("--overwrite", is_flag=True, help="Replace an existing package of the same name.")
@pass_ctx
@_handle_errors
def import_package_cmd(ctx: Context, file: Path, name: str | None, overwrite: bool):
    """Import a portable package JSON file (export or legacy context_vN.json)."""
    pkg = ctx.service.import_package_data(file.read_bytes(), rename=name, overwrite=overwrite)
    console.print(
        f"[green]✓[/] Imported '{pkg.name}' v{pkg.version} ({pkg.memory.total_items} items)"
    )


@main.command()
@click.argument("names", nargs=-1, required=True)
@click.option("--output", "-o", "new_name", required=True, help="Name of the merged package.")
@click.option("--dry-run", is_flag=True, help="Show the merge report without saving.")
@click.option("--overwrite", is_flag=True, help="Replace an existing package of that name.")
@pass_ctx
@_handle_errors
def merge(ctx: Context, names: tuple[str, ...], new_name: str, dry_run: bool, overwrite: bool):
    """Merge packages; near-duplicates are folded and conflicts are flagged."""
    merged, result = ctx.service.merge_packages(
        list(names), new_name, dry_run=dry_run, overwrite=overwrite
    )
    s = result.summary()
    console.print(
        Panel(
            f"Sources: [yellow]{', '.join(s['sources'])}[/]\n"
            f"Items: {s['input_items']} → [cyan]{s['kept_items']}[/] "
            f"({s['dropped_items']} folded as duplicates)\n"
            f"Duplicate groups: {s['duplicate_groups']} · "
            f"Possible conflicts: [{'red' if s['conflicts'] else 'green'}]{s['conflicts']}[/]",
            title="Merge report" + (" (dry run)" if dry_run else ""),
            border_style="cyan",
        )
    )
    for g in result.duplicates:
        console.print(f"[green]≡[/] Kept: {g.kept.content}  [dim](sim ≥ {g.similarity:.2f})[/]")
        for d in g.duplicates:
            console.print(f"    [dim]folded:[/] {d.content}  [dim]← {d.origin}[/]")
    for c in result.conflicts:
        console.print(
            f"[red]⚠[/] [{CATEGORY_LABELS[c.category]}] {c.reason} "
            f"[dim](sim {c.similarity:.2f}; shared: {', '.join(c.shared_terms)})[/]"
        )
        console.print(f"    A: {c.a.content}  [dim]← {c.a.origin}[/]")
        console.print(f"    B: {c.b.content}  [dim]← {c.b.origin}[/]")
    if dry_run:
        console.print("\n[dim]Dry run — nothing saved. Re-run without --dry-run to save.[/]")
    else:
        console.print(f"\n[green]✓[/] Saved merged package '{merged.name}' v{merged.version}")


# ---------------------------------------------------------------------------
# Privacy, quality, storage
# ---------------------------------------------------------------------------


@main.command()
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@pass_ctx
@_handle_errors
def scan(ctx: Context, files: tuple[Path, ...]):
    """Show what the redactor would mask in the given files (nothing is stored)."""
    text, _ = _read_inputs(files)
    report = ctx.service.scan(text)
    if not report.findings:
        console.print("[green]✓[/] No secrets or personal data detected.")
        return
    table = Table(title="Redaction preview", border_style="yellow")
    table.add_column("Kind", style="bold")
    table.add_column("What")
    table.add_column("Preview", style="dim")
    for f in report.findings[:100]:
        table.add_row(f.kind, f.label, f.preview)
    console.print(table)
    console.print(
        f"[yellow]{report.total} finding(s)[/] — these would be replaced with "
        "[bold][REDACTED:KIND][/bold] placeholders on import."
    )


@main.command(name="eval")
@click.option("--json", "as_json", is_flag=True, help="Print the full report as JSON.")
@click.option("--top-k", default=3, show_default=True, type=click.IntRange(1, 20))
def eval_cmd(as_json: bool, top_k: int):
    """Run the offline evaluation suite (fixtures only, no API calls)."""
    from contextbridge.evaluation import run_evaluation

    report = run_evaluation(top_k=top_k)
    if as_json:
        click.echo(report.model_dump_json(indent=2))
        return

    table = Table(title="ContextBridge offline evaluation", border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    labels = {
        "extraction_coverage": "Extraction coverage (rule-based)",
        "extraction_unmatched_ratio": "Extracted items not matching gold",
        "retrieval_precision_at_k": f"Retrieval precision@{top_k}",
        "retrieval_recall_at_k": f"Retrieval recall@{top_k}",
        "retrieval_mrr": "Retrieval MRR",
        "duplicate_f1": "Duplicate detection F1",
        "redaction_recall": "Redaction recall",
        "redaction_false_positives": "Redaction false positives (count)",
        "token_savings_vs_transcript": "Token savings vs. transcript",
        "token_savings_vs_full_memory": "Token savings vs. full memory",
    }
    for key, label in labels.items():
        value = report.aggregate.get(key, 0.0)
        shown = f"{value:.0f}" if key.endswith("false_positives") else f"{value:.1%}"
        table.add_row(label, shown)
    console.print(table)
    console.print(f"[dim]Fixtures: {', '.join(report.fixtures)}[/]")
    for note in report.notes:
        console.print(f"[dim]• {note}[/]")
    console.print("[dim]Use --json for per-query and per-fixture detail.[/]")


@main.command()
@pass_ctx
def info(ctx: Context):
    """Show storage backend, location, and configuration."""
    store = ctx.service.store
    settings = ctx.settings
    lines = [
        f"Backend: [bold]{store.backend_name}[/]",
        f"Location: {store.location()}",
        f"Redaction by default: {'on' if settings.redact_by_default else 'off'}",
        f"Default engine: {settings.default_adapter}",
        f"Packages: {len(store.list_packages())}",
    ]
    if isinstance(store, SQLiteStore):
        st = store.stats()
        lines.append(f"Stored versions: {st['versions']} · DB size: {st['bytes']:,} bytes")
    console.print(Panel("\n".join(lines), title="ContextBridge", border_style="cyan"))


@main.command()
@click.option(
    "--from-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory with legacy JSON packages (default: the storage directory).",
)
@pass_ctx
def migrate(ctx: Context, from_dir: Path | None):
    """Import legacy JSON packages into the SQLite store (JSON files are kept)."""
    store = ctx.service.store
    if not isinstance(store, SQLiteStore):
        _fail("Migration targets the SQLite backend; run without --backend json")
    source_dir = from_dir or ctx.settings.storage_dir
    legacy_names = JSONStore(base_dir=source_dir).list_packages()
    imported = import_legacy_json(source_dir, store)
    already = [n for n in legacy_names if n not in imported]
    if imported:
        console.print(f"[green]✓[/] Imported {len(imported)} package(s): {', '.join(imported)}")
    if already:
        console.print(f"[dim]Already in SQLite: {', '.join(already)}[/]")
    if not legacy_names:
        console.print(f"[dim]No legacy JSON packages found in {source_dir}.[/]")
    console.print("[dim]JSON files are left in place; remove them yourself once you are happy.[/]")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _display_memory(memory: StructuredMemory, *, title: str | None = None, show_ids=False):
    if title:
        console.print(f"\n[bold underline]{title}[/]")
    if memory.total_items == 0:
        console.print("[dim]  (no memory items)[/]")
        return
    for cat in MemoryCategory:
        items = memory.get_category(cat)
        if not items:
            continue
        text = Text()
        text.append(f"\n{_CATEGORY_ICONS[cat]} {CATEGORY_LABELS[cat]}", style="bold")
        text.append(f"  ({len(items)})", style="dim")
        console.print(text)
        for item in items:
            bits = []
            if item.confidence < 1.0:
                bits.append(f"{item.confidence:.0%}")
            if item.was_redacted:
                bits.append("redacted")
            if show_ids:
                bits.append(item.id)
            suffix = f" [dim]({' · '.join(bits)})[/]" if bits else ""
            console.print(f"  • {item.content}{suffix}")


def _source_note(source: str) -> str:
    """Render an item's provenance: a quoted excerpt, or the rule that produced it."""
    if not source:
        return ""
    if source.startswith("rule:"):
        return "\n[dim]detected by rule: " + source[5:].replace("_", " ") + "[/]"
    return "\n[dim]↳ " + source + "[/]"


def _display_redaction(report) -> None:
    if not report.total:
        return
    parts = [f"{n} × {DETECTOR_LABELS.get(k, k)}" for k, n in sorted(report.counts.items())]
    console.print(
        f"\n[yellow]🔒 Redacted {report.total} value(s) in {report.items_redacted} item(s):[/] "
        + ", ".join(parts)
    )


def _display_retrieval(result: RetrievalResult, name: str) -> None:
    s = result.summary()
    table = Table(
        title=f"Retrieval for {name!r} — “{result.query}”",
        border_style="magenta",
        show_lines=True,
    )
    table.add_column("#", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Category", style="yellow")
    table.add_column("Memory")
    table.add_column("Why", style="dim")
    table.add_column("Tok", justify="right", style="dim")
    for sel in result.selected:
        table.add_row(
            str(sel.rank),
            f"{sel.score:.2f}",
            CATEGORY_LABELS[sel.item.category],
            sel.item.content + (f"\n[dim]↳ {sel.item.source}[/]" if sel.item.source else ""),
            sel.reason,
            str(sel.tokens),
        )
    console.print(table)
    if not result.selected:
        console.print("[yellow]No items matched the query.[/] Try broader terms or --min-score 0.")
    saved = s["tokens_saved_vs_full_memory"]
    console.print(
        f"[dim]{s['selected']} of {s['considered']} considered ({s['total_items']} total) · "
        f"method: {s['method']} · ≈ {s['tokens_selected']:,} tokens selected, "
        f"≈ {saved:,} saved vs. full memory ({s['tokens_full_memory']:,})"
        + (f" · {s['dropped_for_budget']} dropped for budget" if s["dropped_for_budget"] else "")
        + "[/]"
    )


if __name__ == "__main__":
    main()
