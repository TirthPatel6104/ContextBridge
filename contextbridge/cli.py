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

Sharing boundaries & lifecycle
    cb share         Set who may see an item (any / local_only / never)
    cb done / reopen Mark open tasks finished, or active again
    cb supersede     Record that one item replaces another
    cb conflicts     Review statements that may contradict stored memory
    cb audit         Egress ledger: which items went to which model, when
    cb health        Stale items, open tasks, exposure, unresolved conflicts

Privacy & quality
    cb scan          Show what the redactor would mask in a file
    cb eval          Run the offline evaluation suite (``--golden`` for the retrieval gate)
    cb info          Storage backend, location, and stats

Serving & storage
    cb serve         Run the FastAPI server (dashboard + JSON API + OpenAPI docs)
    cb migrate       Import legacy JSON packages into SQLite, or copy SQLite → Postgres
    cb db            Postgres schema status / upgrade

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
    ItemStatus,
    MemoryCategory,
    RetrievalOptions,
    RetrievalResult,
    SharingPolicy,
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
SHARING_CHOICES = click.Choice([p.value for p in SharingPolicy], case_sensitive=False)

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
    type=click.Choice(["sqlite", "postgres", "json"], case_sensitive=False),
    envvar="CB_STORAGE_BACKEND",
    default=None,
    help="Storage backend (default sqlite; postgres needs CB_DATABASE_URL).",
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
    for note in outcome.notes:
        console.print(f"[cyan]ℹ[/] {note}")

    verb = "Created" if outcome.created else "Updated"
    seen = f", {outcome.re_seen_items} seen again" if outcome.re_seen_items else ""
    console.print(
        f"\n[green]✓[/] {verb} package [bold]{outcome.package.name}[/] "
        f"→ v{outcome.package.version} "
        f"({outcome.added_items} new{seen}, {outcome.package.memory.total_items} total items)"
    )
    console.print(
        f"[dim]Transcript ≈ {outcome.transcript_tokens:,} tokens · "
        f"stored in {ctx.service.store.location()}[/]"
    )
    if outcome.conflicts:
        _display_pending(outcome.package, outcome.conflicts)


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
@click.option(
    "--dry-run", is_flag=True, default=False, help="Preview without recording in the egress ledger."
)
@pass_ctx
@_handle_errors
def prompt(ctx: Context, name, model, query, top_k, categories, budget, copy, raw, dry_run):
    """Generate a paste-ready context prompt for a web UI or local model.

    Items marked local_only are withheld from ChatGPT / Claude, items marked
    never are withheld from every target, and superseded / done items are
    never rendered.  Each build is recorded in the egress ledger (cb audit).

    \b
    Examples:
      cb prompt project --model claude --copy
      cb prompt project --model openai --query "database schema" --top-k 4
    """
    options = _options_from_flags(top_k, categories, budget, 0.0) if query else None
    built = ctx.service.build_prompt(
        name, model, query=query, options=options, surface="cli", record=not dry_run
    )
    text = built["prompt"]

    if raw:
        click.echo(text)
    else:
        retrieval: RetrievalResult | None = built["retrieval"]
        meta = (
            f"[dim]Package: {name} v{built['version']} · "
            f"{built['included_items']} of {built['total_items']} items · "
            f"≈ {built['tokens_prompt']:,} tokens · target is {built['target_kind']}"
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
        _display_withheld(built["withheld"])
        if dry_run:
            console.print("[dim]Dry run: not recorded in the egress ledger.[/]")
        else:
            console.print(
                f"[dim]Recorded in the egress ledger (event #{built['egress_id']}); "
                f"see [bold]cb audit {name}[/bold].[/]"
            )

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

    system_prompt, info = ctx.service.system_prompt_for_query(
        context, question, model, adapter=adapter, smart=smart, top_k=top_k
    )
    result: RetrievalResult | None = info["retrieval"]
    if result is not None:
        console.print(
            f"[dim]Injecting {len(result.selected)} of {info['total_items']} items "
            f"({result.method} retrieval, ≈ {result.tokens_selected:,} tokens).[/]"
        )
    _display_withheld(info["withheld"])

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
    status = pkg.memory.status_counts()
    protected = sum(1 for i in pkg.memory.all_items if i.sharing != SharingPolicy.ANY)
    pending = ctx.service.pending_conflicts(name)
    console.print(
        Panel(
            f"[bold]{pkg.name}[/] v{pkg.version} (schema v{pkg.schema_version})\n"
            f"Source: [yellow]{pkg.source_model or 'unknown'}[/]\n"
            f"Created: [dim]{pkg.created_at:%Y-%m-%d %H:%M}[/]  "
            f"Updated: [dim]{pkg.updated_at:%Y-%m-%d %H:%M}[/]\n"
            f"Items: [cyan]{pkg.memory.total_items}[/] "
            f"(active {status['active']}, superseded {status['superseded']}, done {status['done']})"
            f"  Redacted: [cyan]{redacted}[/]  Sharing-restricted: [cyan]{protected}[/]"
            + (f"  Pending conflicts: [red]{len(pending)}[/]" if pending else ""),
            title="Package",
            border_style="cyan",
        )
    )
    _display_memory(pkg.memory, show_ids=ids)
    if pending:
        console.print(
            f"\n[dim]Run [bold]cb conflicts {name}[/bold] to review pending conflicts.[/]"
        )


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
    table.add_column("Changed", style="yellow", justify="center")
    for v in ctx.service.packager.get_version_summary(pkg):
        table.add_row(
            f"v{v['version']}",
            v["timestamp"][:19].replace("T", " "),
            v["source_model"] or "—",
            v["note"] or "—",
            str(v["added"]),
            str(v["removed"]),
            str(v.get("modified", 0)),
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
# Sharing boundaries & lifecycle
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.argument("item_ids", nargs=-1, required=True)
@click.option(
    "--policy",
    "-p",
    type=SHARING_CHOICES,
    required=True,
    help="any: every target · local_only: withheld from ChatGPT/Claude · never: no prompt.",
)
@pass_ctx
@_handle_errors
def share(ctx: Context, name: str, item_ids: tuple[str, ...], policy: str):
    """Set who may see items when a prompt is built (see ``cb inspect --ids``)."""
    pkg, changed = ctx.service.set_sharing(name, item_ids, policy)
    if changed:
        console.print(
            f"[green]✓[/] {changed} item(s) now [bold]{policy}[/] → v{pkg.version}. "
            f"Withheld items are listed every time a prompt is built."
        )
    else:
        console.print("[dim]Nothing changed: those items already had that policy.[/]")


@main.command()
@click.argument("name")
@click.argument("item_ids", nargs=-1, required=True)
@pass_ctx
@_handle_errors
def done(ctx: Context, name: str, item_ids: tuple[str, ...]):
    """Mark items (typically open tasks) as done; they stay in history but leave prompts."""
    pkg, changed = ctx.service.set_status(name, item_ids, ItemStatus.DONE)
    console.print(f"[green]✓[/] Marked {changed} item(s) done → v{pkg.version}")


@main.command()
@click.argument("name")
@click.argument("item_ids", nargs=-1, required=True)
@pass_ctx
@_handle_errors
def reopen(ctx: Context, name: str, item_ids: tuple[str, ...]):
    """Make done or superseded items active again."""
    pkg, changed = ctx.service.set_status(name, item_ids, ItemStatus.ACTIVE)
    console.print(f"[green]✓[/] Reactivated {changed} item(s) → v{pkg.version}")


@main.command()
@click.argument("name")
@click.argument("old_id")
@click.argument("new_id")
@pass_ctx
@_handle_errors
def supersede(ctx: Context, name: str, old_id: str, new_id: str):
    """Record that NEW_ID replaces OLD_ID. The old item is kept, marked superseded."""
    pkg = ctx.service.supersede(name, old_id, new_id)
    console.print(f"[green]✓[/] {old_id} is now superseded by {new_id} → v{pkg.version}")


@main.command()
@click.argument("name")
@click.option(
    "--resolve",
    nargs=3,
    metavar="EXISTING_ID INCOMING_ID ACTION",
    default=None,
    help="Settle one conflict: ACTION is keep_new, keep_old, or dismiss.",
)
@pass_ctx
@_handle_errors
def conflicts(ctx: Context, name: str, resolve):
    """Review new statements that may contradict what a package already holds."""
    if resolve:
        existing_id, incoming_id, action = resolve
        pkg = ctx.service.resolve_conflict(name, existing_id, incoming_id, action)
        console.print(f"[green]✓[/] Resolved ({action}) → v{pkg.version}")
    pending = ctx.service.pending_conflicts(name)
    if not pending:
        console.print("[green]✓[/] No pending conflicts.")
        return
    _display_pending(ctx.service.get_package(name), pending)
    console.print(
        "\n[dim]Resolve with [bold]cb conflicts NAME --resolve EXISTING_ID INCOMING_ID "
        "keep_new|keep_old|dismiss[/bold].[/]"
    )


@main.command()
@click.argument("name")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 1000))
@click.option("--json", "as_json", is_flag=True, help="Print the full ledger as JSON.")
@click.option("--clear", is_flag=True, help="Delete the ledger for this package.")
@pass_ctx
@_handle_errors
def audit(ctx: Context, name: str, limit: int, as_json: bool, clear: bool):
    """Egress ledger: which memory items were rendered for which model, and when."""
    if clear:
        removed = ctx.service.clear_audit(name)
        console.print(f"[green]✓[/] Cleared {removed} ledger entr{'y' if removed == 1 else 'ies'}")
        return
    report = ctx.service.audit(name, limit=limit)
    if as_json:
        import json

        click.echo(json.dumps(report, indent=2))
        return
    s = report["summary"]
    console.print(
        Panel(
            f"Events: [cyan]{s['events']}[/] · Items ever shared: [cyan]{s['items_shared']}[/] · "
            f"Shared with a cloud model: [{'red' if s['items_shared_with_cloud'] else 'green'}]"
            f"{s['items_shared_with_cloud']}[/] · "
            f"Never shared: [green]{s['items_never_shared']}[/]\n"
            f"Targets: {', '.join(s['targets']) or '—'}",
            title=f"Egress ledger — {name}",
            border_style="cyan",
        )
    )
    if not report["events"]:
        console.print("[dim]No prompts have been built for this package yet.[/]")
        return
    table = Table(border_style="cyan")
    table.add_column("#", justify="right")
    table.add_column("When", style="dim")
    table.add_column("Target", style="yellow")
    table.add_column("Kind")
    table.add_column("Via")
    table.add_column("Items", justify="right")
    table.add_column("Withheld", justify="right")
    table.add_column("Tokens", justify="right", style="dim")
    table.add_column("Query", style="dim")
    for e in report["events"]:
        table.add_row(
            str(e["id"]),
            e["timestamp"][:16].replace("T", " "),
            e["target_model"],
            "[red]cloud[/]" if e["target_kind"] == "cloud" else "[green]local[/]",
            e["surface"],
            str(e["item_count"]),
            str(e["withheld_count"]),
            f"{e['tokens']:,}",
            (e["query"] or "—")[:40],
        )
    console.print(table)
    cloud = [i for i in report["items"] if i["sent_to_cloud"]]
    if cloud:
        console.print(f"\n[bold]Items that have been sent to a cloud model ({len(cloud)}):[/]")
        for i in cloud[:limit]:
            targets = ", ".join(f"{t} ×{n}" for t, n in i["targets"].items())
            gone = "" if i["present"] else " [dim](no longer in package)[/]"
            console.print(f"  • {i['content']}{gone}  [dim]({targets}; id {i['id']})[/]")


@main.command()
@click.argument("name")
@click.option("--stale-days", default=30, show_default=True, type=click.IntRange(0, 3650))
@click.option("--json", "as_json", is_flag=True, help="Print the full report as JSON.")
@pass_ctx
@_handle_errors
def health(ctx: Context, name: str, stale_days: int, as_json: bool):
    """Memory health: stale items, open tasks, corroboration, exposure, conflicts."""
    report = ctx.service.health(name, stale_days=stale_days)
    if as_json:
        import json

        click.echo(json.dumps(report, indent=2))
        return
    c = report["counts"]
    console.print(
        Panel(
            f"Items: [cyan]{c['items']}[/] (active {c['status_active']}, "
            f"superseded {c['status_superseded']}, done {c['status_done']})\n"
            f"Corroborated by ≥2 imports: [green]{c['corroborated']}[/] · "
            f"single sighting: {c['single_sighting']}\n"
            f"Stale (not seen for {stale_days}+ days): "
            f"[{'yellow' if c['stale'] else 'green'}]{c['stale']}[/] · "
            f"Open tasks: {c['open_tasks']} · "
            f"Pending conflicts: [{'red' if c['pending_conflicts'] else 'green'}]"
            f"{c['pending_conflicts']}[/]\n"
            f"Shared with a cloud model: [{'red' if c['shared_with_cloud'] else 'green'}]"
            f"{c['shared_with_cloud']}[/] · never shared anywhere: {c['never_shared']} · "
            f"egress events: {c['egress_events']}",
            title=f"Memory health — {name} v{report['version']}",
            border_style="cyan",
        )
    )
    if report["stale_items"]:
        console.print("\n[bold yellow]Stale items[/] (confirm, supersede, or remove):")
        for i in report["stale_items"][:20]:
            console.print(
                f"  • {i['content']}  [dim]({i['days_since_seen']} days; id {i['id']})[/]"
            )
    if report["open_tasks"]:
        console.print("\n[bold]Open tasks[/] (cb done NAME ID when finished):")
        for i in report["open_tasks"][:20]:
            console.print(f"  • {i['content']}  [dim](id {i['id']})[/]")
    if report["handoffs"]:
        console.print(f"\n[dim]Hand-offs recorded: {len(report['handoffs'])}[/]")


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
@click.option("--golden", is_flag=True, help="Run the golden retrieval harness instead.")
@click.option(
    "--check-baseline",
    is_flag=True,
    help="With --golden: exit 1 when a guarded metric regressed against the baseline.",
)
@click.option(
    "--baseline",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Baseline JSON to compare against (default: the bundled golden_baseline.json).",
)
@click.option(
    "--update-baseline",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="With --golden: write the current aggregate as a new baseline file.",
)
@click.option("--tolerance", default=0.02, show_default=True, type=click.FloatRange(0, 1))
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write the full JSON report to this file (for CI artifacts).",
)
def eval_cmd(
    as_json: bool,
    top_k: int,
    golden: bool,
    check_baseline: bool,
    baseline: Path | None,
    update_baseline: Path | None,
    tolerance: float,
    output: Path | None,
):
    """Run the offline evaluation suite (fixtures only, no API calls)."""
    if golden:
        _eval_golden(as_json, top_k, check_baseline, baseline, update_baseline, tolerance, output)
        return
    from contextbridge.evaluation import run_evaluation

    report = run_evaluation(top_k=top_k)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
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


def _eval_golden(
    as_json: bool,
    top_k: int,
    check_baseline: bool,
    baseline: Path | None,
    update_baseline: Path | None,
    tolerance: float,
    output: Path | None,
) -> None:
    import json

    from contextbridge.evaluation.golden import (
        baseline_from_report,
        compare_to_baseline,
        load_baseline,
        run_golden,
    )

    report = run_golden(top_k=top_k)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if update_baseline is not None:
        update_baseline.parent.mkdir(parents=True, exist_ok=True)
        update_baseline.write_text(
            json.dumps(baseline_from_report(report), indent=2) + "\n", encoding="utf-8"
        )
        console.print(f"[green]✓[/] Baseline written to {update_baseline}")
    if as_json:
        click.echo(report.model_dump_json(indent=2))
    else:
        table = Table(
            title=f"Golden retrieval harness — {report.pairs} pairs, top_k={top_k}",
            border_style="cyan",
        )
        table.add_column("Slice", style="bold")
        table.add_column("Pairs", justify="right")
        table.add_column("Hit@1", justify="right")
        table.add_column(f"P@{top_k}", justify="right")
        table.add_column(f"R@{top_k}", justify="right")
        table.add_column("MRR", justify="right")
        table.add_column(f"nDCG@{top_k}", justify="right")

        def row(label: str, m: dict, n: int) -> None:
            table.add_row(
                label,
                str(n),
                f"{m['hit_at_1']:.1%}",
                f"{m['precision_at_k']:.1%}",
                f"{m['recall_at_k']:.1%}",
                f"{m['mrr']:.1%}",
                f"{m['ndcg_at_k']:.1%}",
            )

        row("all", report.aggregate, report.pairs)
        for tag, m in report.by_tag.items():
            row(tag, m, int(m.get("pairs", 0)))
        console.print(table)
        misses = report.failures()
        if misses:
            console.print(f"[dim]{len(misses)} query(ies) found nothing relevant in the top-k:[/]")
            for f in misses[:12]:
                console.print(f"[dim]  • {f.id} ({', '.join(f.tags)}): {f.query}[/]")
        for note in report.notes:
            console.print(f"[dim]• {note}[/]")

    if check_baseline:
        base = load_baseline(baseline)
        if base is None:
            _fail("No baseline found to compare against")
        regressions = compare_to_baseline(report, base, tolerance=tolerance)
        if regressions:
            console.print(f"[red]✗ {len(regressions)} metric(s) regressed beyond {tolerance}:[/]")
            for r in regressions:
                console.print(
                    f"  {r['metric']}: baseline {r['baseline']:.4f} → now {r['current']:.4f} "
                    f"({r['delta']:+.4f})"
                )
            sys.exit(1)
        console.print(
            f"[green]✓[/] No regression against the baseline "
            f"(tolerance {tolerance}, {report.pairs} pairs)."
        )


@main.command()
@click.option("--host", default=None, help="Bind address (default: CB_HOST or 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Port (default: CB_PORT or 5000).")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (development).")
def serve(host: str | None, port: int | None, reload: bool):
    """Run the FastAPI server: dashboard, JSON API, OpenAPI docs at /docs."""
    try:
        from contextbridge.api.app import run as run_server
    except ImportError:  # pragma: no cover - optional dependency missing
        _fail("The API server needs the 'api' extra: pip install 'contextbridge[api]'")
    settings = Settings.from_env()
    console.print(
        f"[bold]ContextBridge API[/] → http://{host or settings.host}:{port or settings.port} "
        f"[dim](docs at /docs · backend {settings.storage_backend})[/]"
    )
    run_server(host=host, port=port, reload=reload)


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
    stats = getattr(store, "stats", None)
    if callable(stats):
        st = stats()
        lines.append(
            f"Stored versions: {st.get('versions', 0)} · size: {st.get('bytes', 0):,} bytes"
        )
        if "embeddings" in st:
            lines.append(
                f"Schema version: {st.get('schema_version')} · pgvector {st.get('pgvector')} · "
                f"embeddings stored: {st['embeddings']}"
            )
    console.print(Panel("\n".join(lines), title="ContextBridge", border_style="cyan"))


@main.command()
@click.option(
    "--from-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory with legacy JSON packages (default: the storage directory).",
)
@click.option(
    "--to",
    "target",
    type=click.Choice(["sqlite", "postgres"], case_sensitive=False),
    default="sqlite",
    show_default=True,
    help="sqlite: import legacy JSON. postgres: copy the SQLite store to PostgreSQL.",
)
@click.option(
    "--database-url",
    default=None,
    help="PostgreSQL DSN for --to postgres (default: CB_DATABASE_URL).",
)
@click.option(
    "--overwrite", is_flag=True, help="Replace packages that already exist in the target."
)
@click.option("--only", "names", multiple=True, help="Copy only these packages (repeatable).")
@pass_ctx
def migrate(
    ctx: Context,
    from_dir: Path | None,
    target: str,
    database_url: str | None,
    overwrite: bool,
    names: tuple[str, ...],
):
    """Move data between backends without deleting anything.

    ``cb migrate`` imports legacy JSON packages into SQLite.  ``cb migrate --to
    postgres`` copies every package (all versions plus the egress ledger) from
    the local SQLite store into PostgreSQL, so a deployment can start from
    your existing memory.
    """
    if target.lower() == "postgres":
        from contextbridge.storage import copy_store

        dsn = database_url or ctx.settings.database_url
        if not dsn:
            _fail("Give --database-url or set CB_DATABASE_URL")
        source = ctx.service.store
        if source.backend_name == "postgres":
            _fail("The source is already Postgres; run with --backend sqlite")
        try:
            pg = get_store("postgres", settings=ctx.settings, database_url=dsn)
        except Exception as exc:
            _fail(f"Could not open PostgreSQL: {exc.__class__.__name__}: {exc}")
        try:
            report = copy_store(source, pg, overwrite=overwrite, names=list(names) or None)
        finally:
            pg.close()
        if report["copied"]:
            console.print(
                f"[green]✓[/] Copied {len(report['copied'])} package(s): "
                + ", ".join(report["copied"])
            )
        if report["skipped"]:
            console.print(
                f"[dim]Already in Postgres (use --overwrite): {', '.join(report['skipped'])}[/]"
            )
        if report["failed"]:
            _fail(f"Failed: {', '.join(report['failed'])}")
        if not (report["copied"] or report["skipped"]):
            console.print("[dim]No packages to copy.[/]")
        console.print("[dim]The SQLite store is untouched.[/]")
        return

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


@main.group()
def db():
    """PostgreSQL schema management (pgvector backend)."""


def _open_pg(ctx: Context, database_url: str | None):
    dsn = database_url or ctx.settings.database_url
    if not dsn:
        _fail("Give --database-url or set CB_DATABASE_URL")
    try:
        return get_store("postgres", settings=ctx.settings, database_url=dsn, auto_migrate=False)
    except Exception as exc:
        _fail(f"Could not open PostgreSQL: {exc.__class__.__name__}: {exc}")


@db.command(name="status")
@click.option("--database-url", default=None, help="PostgreSQL DSN (default: CB_DATABASE_URL).")
@pass_ctx
def db_status(ctx: Context, database_url: str | None):
    """Show the applied schema version and pending migrations."""
    store = _open_pg(ctx, database_url)
    try:
        status = store.schema_status()
        st = store.stats() if status["schema_version"] >= 2 else None
    finally:
        store.close()
    lines = [
        f"Location: {status['location']}",
        f"Schema version: {status['schema_version']} (latest {status['latest']})",
        f"Pending migrations: {status['pending']}",
        f"pgvector: {status['pgvector_installed'] or 'not installed'}"
        + ("" if status["pgvector_available"] else " [red](extension not available!)[/]"),
    ]
    if st is not None:
        lines.append(
            f"Packages: {st['packages']} · versions: {st['versions']} · "
            f"embeddings: {st['embeddings']} · ledger events: {st['egress_events']}"
        )
    console.print(Panel("\n".join(lines), title="PostgreSQL", border_style="cyan"))
    if status["pending"] > 0:
        console.print("[yellow]Run 'cb db upgrade' to apply pending migrations.[/]")


@db.command(name="upgrade")
@click.option("--database-url", default=None, help="PostgreSQL DSN (default: CB_DATABASE_URL).")
@pass_ctx
def db_upgrade(ctx: Context, database_url: str | None):
    """Apply pending schema migrations."""
    store = _open_pg(ctx, database_url)
    try:
        ran = store.migrate()
    finally:
        store.close()
    if ran:
        console.print(f"[green]✓[/] Applied {len(ran)} migration(s): {', '.join(ran)}")
    else:
        console.print("[green]✓[/] Schema is up to date.")


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
            if item.sharing == SharingPolicy.LOCAL_ONLY:
                bits.append("[yellow]local only[/]")
            elif item.sharing == SharingPolicy.NEVER:
                bits.append("[red]never share[/]")
            if item.status == ItemStatus.SUPERSEDED:
                bits.append(f"[red]superseded[/] by {item.superseded_by}")
            elif item.status == ItemStatus.DONE:
                bits.append("[green]done[/]")
            if item.seen_count > 1:
                bits.append(f"seen ×{item.seen_count}")
            if show_ids:
                bits.append(item.id)
            suffix = f" [dim]({' · '.join(bits)})[/]" if bits else ""
            marker = "  • " if item.is_active else "  ◦ "
            style_open, style_close = ("[dim]", "[/]") if not item.is_active else ("", "")
            console.print(f"{marker}{style_open}{item.content}{style_close}{suffix}")


def _display_withheld(withheld) -> None:
    if not withheld:
        return
    console.print(f"\n[yellow]⛔ {len(withheld)} item(s) withheld from this target:[/]")
    for w in withheld:
        console.print(f"  ◦ {w.item.content}  [dim]({w.reason}; id {w.item.id})[/]")


def _display_pending(pkg, pending) -> None:
    by_id = pkg.memory.items_by_id()
    console.print(f"\n[bold red]⚠ {len(pending)} statement(s) may contradict stored memory[/]")
    for p in pending:
        a, b = by_id.get(p.existing_id), by_id.get(p.incoming_id)
        if a is None or b is None:
            continue
        console.print(
            f"[{CATEGORY_LABELS[p.category]}] {p.reason} "
            f"[dim](sim {p.similarity:.2f}; shared: {', '.join(p.shared_terms)})[/]"
        )
        console.print(f"    stored:   {a.content}  [dim]← {a.origin} · id {a.id}[/]")
        console.print(f"    incoming: {b.content}  [dim]← {b.origin} · id {b.id}[/]")


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
