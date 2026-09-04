"""
ContextBridge CLI — command-line interface for cross-model context management.

Commands:
    cb export      Extract context from a chat transcript
    cb web-export  Export from web chat (paste or file, with auto-prompt)
    cb prompt      Generate paste-ready context for web ChatGPT/Claude
    cb query       Ask a question with relevant context injected
    cb history     View version history of a context package
    cb rollback    Roll back to a previous version
    cb list        List all stored context packages
    cb inspect     Inspect a context package
    cb delete      Delete a stored context package
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from contextbridge.adapters import get_adapter
from contextbridge.core.memory import MemoryExtractor
from contextbridge.core.packager import ContextPackager
from contextbridge.core.prompt_builder import PromptBuilder
from contextbridge.core.retriever import MemoryRetriever
from contextbridge.storage.json_store import JSONStore
from contextbridge.storage.vector_store import VectorStore

console = Console()
packager = ContextPackager()
builder = PromptBuilder()


def _get_store() -> JSONStore:
    return JSONStore()


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Main group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version="0.1.0", prog_name="ContextBridge")
def main():
    """
    ContextBridge — Cross-model conversational memory system.

    Extract, transfer, and query conversational context across LLMs.
    """
    pass


# ---------------------------------------------------------------------------
# cb export
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--model",
    "-m",
    required=True,
    type=click.Choice(["openai", "claude", "local"], case_sensitive=False),
    help="LLM adapter to use for extraction.",
)
@click.option(
    "--chat",
    "-c",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to the chat transcript file.",
)
@click.option(
    "--output",
    "-o",
    required=True,
    help="Name for the context package.",
)
def export(model: str, chat: Path, output: str):
    """Extract structured context from a chat transcript."""
    console.print(
        Panel(
            f"[bold cyan]Exporting context[/]\n"
            f"Model: [yellow]{model}[/]  •  Chat: [yellow]{chat}[/]  •  "
            f"Output: [yellow]{output}[/]",
            title="ContextBridge Export",
            border_style="cyan",
        )
    )

    raw_text = chat.read_text(encoding="utf-8")
    adapter = get_adapter(model)
    extractor = MemoryExtractor()

    with console.status("[bold green]Extracting structured memory..."):
        memory = _run_async(extractor.extract(raw_text, adapter, source_model=model))

    # Display extraction results
    _display_memory_summary(memory)

    # Package and save
    store = _get_store()
    if store.exists(output):
        existing = store.load(output)
        pkg = packager.update(existing, memory, source_model=model)
        console.print(f"\n[green]✓[/] Updated existing package → v{pkg.version}")
    else:
        pkg = packager.create(output, memory, source_model=model)
        console.print(f"\n[green]✓[/] Created new package v{pkg.version}")

    store.save(pkg)
    console.print(f"[dim]Saved to {store.base_dir / output}[/]")


# ---------------------------------------------------------------------------
# cb query
# ---------------------------------------------------------------------------


@main.command()
@click.argument("question")
@click.option(
    "--context",
    "-c",
    required=True,
    help="Name of the context package to use.",
)
@click.option(
    "--model",
    "-m",
    required=True,
    type=click.Choice(["openai", "claude", "local"], case_sensitive=False),
    help="Target LLM adapter for the query.",
)
@click.option(
    "--smart/--no-smart",
    default=True,
    help="Use semantic retrieval to inject only relevant context.",
)
def query(question: str, context: str, model: str, smart: bool):
    """Ask a question with context injected from a stored package."""
    store = _get_store()
    if not store.exists(context):
        console.print(f"[red]✗ Package '{context}' not found[/]")
        sys.exit(1)

    pkg = store.load(context)
    adapter = get_adapter(model)

    if smart and pkg.memory.total_items > 0:
        console.print("[dim]Using semantic retrieval for relevant context...[/]")
        vs = VectorStore(dimension=256)
        retriever = MemoryRetriever(vs)
        _run_async(retriever.index(pkg.memory, adapter))
        filtered_memory = _run_async(
            retriever.query_and_filter(question, pkg.memory, adapter, top_k=5)
        )
        system_prompt = builder.build_from_memory(filtered_memory, model)
    else:
        system_prompt = builder.build(pkg, model)

    console.print(
        Panel(
            f"[bold]Query:[/] {question}\n"
            f"[dim]Context: {context} v{pkg.version} • Model: {model}[/]",
            title="ContextBridge Query",
            border_style="magenta",
        )
    )

    with console.status("[bold green]Querying model..."):
        response = _run_async(adapter.send(question, system_prompt=system_prompt))

    console.print(Panel(response, title="Response", border_style="green"))


# ---------------------------------------------------------------------------
# cb history
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def history(name: str):
    """View version history of a context package."""
    store = _get_store()
    if not store.exists(name):
        console.print(f"[red]✗ Package '{name}' not found[/]")
        sys.exit(1)

    pkg = store.load(name)
    versions = packager.get_version_summary(pkg)

    table = Table(title=f"Version History — {name}", border_style="cyan")
    table.add_column("Version", style="bold", justify="center")
    table.add_column("Timestamp", style="dim")
    table.add_column("Source Model", style="yellow")
    table.add_column("Added", style="green", justify="center")
    table.add_column("Removed", style="red", justify="center")

    for v in versions:
        table.add_row(
            f"v{v['version']}",
            v["timestamp"][:19],
            v["source_model"] or "—",
            str(v["added"]),
            str(v["removed"]),
        )

    console.print(table)
    console.print(
        f"\n[dim]Current version: v{pkg.version} • Total items: {pkg.memory.total_items}[/]"
    )


# ---------------------------------------------------------------------------
# cb rollback
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.option("--version", "-v", required=True, type=int, help="Target version.")
def rollback(name: str, version: int):
    """Roll back a context package to a previous version."""
    store = _get_store()
    if not store.exists(name):
        console.print(f"[red]✗ Package '{name}' not found[/]")
        sys.exit(1)

    pkg = store.load(name)
    old_version = pkg.version

    try:
        pkg = packager.rollback(pkg, version)
    except ValueError as e:
        console.print(f"[red]✗ {e}[/]")
        sys.exit(1)

    store.save(pkg)
    console.print(f"[green]✓[/] Rolled back '{name}' from v{old_version} → v{pkg.version}")


# ---------------------------------------------------------------------------
# cb list
# ---------------------------------------------------------------------------


@main.command(name="list")
def list_packages():
    """List all stored context packages."""
    store = _get_store()
    packages = store.list_packages()

    if not packages:
        console.print("[dim]No context packages found.[/]")
        return

    table = Table(title="Stored Context Packages", border_style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Version", justify="center")
    table.add_column("Source Model", style="yellow")
    table.add_column("Items", justify="center")
    table.add_column("Updated", style="dim")

    for name in packages:
        pkg = store.load(name)
        table.add_row(
            name,
            f"v{pkg.version}",
            pkg.source_model or "—",
            str(pkg.memory.total_items),
            pkg.updated_at.strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# cb delete
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to delete this package?")
def delete(name: str):
    """Delete a stored context package."""
    store = _get_store()
    if not store.exists(name):
        console.print(f"[red]✗ Package '{name}' not found[/]")
        sys.exit(1)

    store.delete(name)
    console.print(f"[green]✓[/] Deleted package '{name}'")


# ---------------------------------------------------------------------------
# cb inspect
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
def inspect(name: str):
    """Inspect the contents of a context package."""
    store = _get_store()
    if not store.exists(name):
        console.print(f"[red]✗ Package '{name}' not found[/]")
        sys.exit(1)

    pkg = store.load(name)

    console.print(
        Panel(
            f"[bold]{pkg.name}[/] v{pkg.version}\n"
            f"Source: [yellow]{pkg.source_model or 'unknown'}[/]\n"
            f"Created: [dim]{pkg.created_at.strftime('%Y-%m-%d %H:%M')}[/]\n"
            f"Updated: [dim]{pkg.updated_at.strftime('%Y-%m-%d %H:%M')}[/]\n"
            f"Total items: [cyan]{pkg.memory.total_items}[/]",
            title="Package Info",
            border_style="cyan",
        )
    )

    _display_memory_summary(pkg.memory)


# ---------------------------------------------------------------------------
# cb prompt  —  generate paste-ready context for web UIs
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.option(
    "--model",
    "-m",
    required=True,
    type=click.Choice(["openai", "claude", "local"], case_sensitive=False),
    help="Target model (affects formatting).",
)
@click.option(
    "--copy",
    is_flag=True,
    default=False,
    help="Copy the prompt to clipboard.",
)
@click.option(
    "--raw",
    is_flag=True,
    default=False,
    help="Print raw prompt without Rich formatting (for piping).",
)
def prompt(name: str, model: str, copy: bool, raw: bool):
    """Generate a paste-ready context prompt for web ChatGPT / Claude.

    \b
    Workflow:
      1. cb export --model openai --chat mychat.txt --output project
      2. cb prompt project --model claude --copy
      3. Paste into Claude/ChatGPT web UI as your first message
    """
    store = _get_store()
    if not store.exists(name):
        console.print(f"[red]✗ Package '{name}' not found[/]")
        sys.exit(1)

    pkg = store.load(name)
    context_block = builder.build(pkg, model)

    if not context_block.strip():
        console.print("[yellow]⚠ Package has no memory items — nothing to generate[/]")
        sys.exit(1)

    # Build the full paste-ready message
    target_name = "Claude" if "claude" in model.lower() else "ChatGPT"
    paste_prompt = (
        f"I'm continuing a conversation from another AI assistant. "
        f"Below is structured context extracted from our previous chats. "
        f"Please internalise this as established knowledge and use it to "
        f"maintain continuity.\n\n"
        f"{context_block}\n\n"
        f"---\n"
        f"Please confirm you've understood this context, then I'll continue with my questions."
    )

    if raw:
        # Raw mode — just print the text, no formatting (good for piping)
        click.echo(paste_prompt)
    else:
        console.print(
            Panel(
                f"[bold green]Paste-ready prompt for {target_name}[/]\n"
                f"[dim]Package: {name} v{pkg.version} • {pkg.memory.total_items} memory items[/]",
                title="ContextBridge → Web UI",
                border_style="green",
            )
        )
        console.print()
        console.print(Panel(paste_prompt, border_style="dim", padding=(1, 2)))

    if copy:
        try:
            import subprocess

            # Windows clipboard
            process = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            process.communicate(paste_prompt.encode("utf-16le"))
            console.print("\n[green]✓[/] Copied to clipboard! Paste into your web chat.")
        except Exception:
            try:
                # macOS fallback
                process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                process.communicate(paste_prompt.encode("utf-8"))
                console.print("\n[green]✓[/] Copied to clipboard!")
            except Exception:
                console.print(
                    "\n[yellow]⚠ Could not copy to clipboard. "
                    "Select and copy the text above manually.[/]"
                )
    elif not raw:
        console.print(
            "\n[dim]Tip: Use --copy to auto-copy to clipboard, or --raw to pipe to a file.[/]"
        )


# ---------------------------------------------------------------------------
# cb web-export  —  interactive export for web chat copy-paste
# ---------------------------------------------------------------------------


@main.command(name="web-export")
@click.option(
    "--model",
    "-m",
    required=True,
    type=click.Choice(["openai", "claude", "local"], case_sensitive=False),
    help="LLM adapter to use for extraction (needs API key).",
)
@click.option(
    "--output",
    "-o",
    required=True,
    help="Name for the context package.",
)
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
    type=click.Choice(["openai", "claude"], case_sensitive=False),
    default=None,
    help="Immediately generate prompt for this target model after export.",
)
def web_export(model: str, output: str, chat: Path | None, target: str | None):
    """Export context from a web chat — optimised for copy-paste workflow.

    \b
    Quick workflow (paste mode):
      1. Copy your entire chat from ChatGPT/Claude web
      2. Run: cb web-export --model openai --output myproject
      3. Paste your chat and press Ctrl+Z (Win) or Ctrl+D (Mac) then Enter
      4. Optionally add --target claude to get a paste-ready prompt immediately

    \b
    File mode:
      1. Save your web chat to a .txt file
      2. Run: cb web-export --model openai --chat mychat.txt --output myproject
    """
    if chat:
        raw_text = chat.read_text(encoding="utf-8")
        console.print(f"[dim]Read {len(raw_text)} chars from {chat}[/]")
    else:
        console.print(
            Panel(
                "[bold cyan]Paste your web chat below[/]\n"
                "[dim]Copy your entire conversation from ChatGPT or Claude,\n"
                "paste it here, then press Ctrl+Z (Windows) or Ctrl+D (Mac) followed by Enter.[/]",
                title="ContextBridge Web Export",
                border_style="cyan",
            )
        )
        raw_text = click.get_text_stream("stdin").read()

    if not raw_text.strip():
        console.print("[red]✗ No chat content provided[/]")
        sys.exit(1)

    console.print(f"[dim]Processing {len(raw_text)} chars...[/]")

    adapter = get_adapter(model)
    extractor = MemoryExtractor()

    with console.status("[bold green]Extracting structured memory..."):
        memory = _run_async(extractor.extract(raw_text, adapter, source_model=model))

    _display_memory_summary(memory)

    # Package and save
    store = _get_store()
    if store.exists(output):
        existing = store.load(output)
        pkg = packager.update(existing, memory, source_model=model)
        console.print(f"\n[green]✓[/] Updated package → v{pkg.version}")
    else:
        pkg = packager.create(output, memory, source_model=model)
        console.print(f"\n[green]✓[/] Created package v{pkg.version}")

    store.save(pkg)
    console.print(f"[dim]Saved to {store.base_dir / output}[/]")

    # Optionally generate prompt for target model
    if target:
        console.print()
        target_name = "Claude" if "claude" in target.lower() else "ChatGPT"
        context_block = builder.build(pkg, target)
        paste_prompt = (
            f"I'm continuing a conversation from another AI assistant. "
            f"Below is structured context extracted from our previous chats. "
            f"Please internalise this as established knowledge and use it to "
            f"maintain continuity.\n\n"
            f"{context_block}\n\n"
            f"---\n"
            f"Please confirm you've understood this context, then I'll continue with my questions."
        )
        console.print(
            Panel(
                paste_prompt,
                title=f"Paste this into {target_name} ↓",
                border_style="green",
                padding=(1, 2),
            )
        )

        try:
            import subprocess

            process = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            process.communicate(paste_prompt.encode("utf-16le"))
            console.print("[green]✓[/] Also copied to clipboard!")
        except Exception:
            console.print("[dim]Tip: Use cb prompt to copy to clipboard later.[/]")

    else:
        console.print(
            f"\n[dim]Next: run [bold]cb prompt {output} --model claude --copy[/bold] "
            f"to get a paste-ready prompt for your target model.[/]"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_memory_summary(memory):
    """Display a rich summary of structured memory."""
    from contextbridge.models import MemoryCategory

    categories = {
        MemoryCategory.IDENTITY: ("👤", "Identity"),
        MemoryCategory.PROJECTS: ("📁", "Projects"),
        MemoryCategory.FACTS: ("📌", "Facts"),
        MemoryCategory.DECISIONS: ("⚖️", "Decisions"),
        MemoryCategory.OPEN_TASKS: ("📋", "Open Tasks"),
        MemoryCategory.PREFERENCES: ("⚙️", "Preferences"),
    }

    for cat, (emoji, label) in categories.items():
        items = memory.get_category(cat)
        if not items:
            continue

        text = Text()
        text.append(f"\n{emoji} {label}", style="bold")
        text.append(f"  ({len(items)} items)", style="dim")
        console.print(text)

        for item in items:
            confidence = f"[dim]({item.confidence:.0%})[/]" if item.confidence < 1.0 else ""
            console.print(f"  • {item.content} {confidence}")


if __name__ == "__main__":
    main()
