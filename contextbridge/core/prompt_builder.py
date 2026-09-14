"""
Prompt Builder — renders context packages for a target model.

Adapts the output format to the target model's conventions (XML tags for
Claude, Markdown headers for GPT, plain numbered text for local models) and
produces the paste-ready wrapper used by the CLI, dashboard, and extension.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from contextbridge.models import (
    ContextPackage,
    MemoryCategory,
    RetrievalResult,
    StructuredMemory,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model-specific formatters
# ---------------------------------------------------------------------------

_CATEGORY_LABELS = {
    MemoryCategory.IDENTITY: "User Identity",
    MemoryCategory.PROJECTS: "Active Projects",
    MemoryCategory.FACTS: "Established Facts",
    MemoryCategory.DECISIONS: "Decisions Made",
    MemoryCategory.OPEN_TASKS: "Open Tasks / TODOs",
    MemoryCategory.PREFERENCES: "Preferences",
}


def _format_markdown(memory: StructuredMemory) -> str:
    """Format memory as Markdown (suitable for GPT / general models)."""
    sections: list[str] = []
    for cat in MemoryCategory:
        items = memory.get_category(cat)
        if not items:
            continue
        lines = [f"## {_CATEGORY_LABELS[cat]}"]
        lines.extend(f"- {item.content}" for item in items)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_xml(memory: StructuredMemory) -> str:
    """Format memory as XML tags (preferred by Claude)."""
    sections: list[str] = []
    for cat in MemoryCategory:
        items = memory.get_category(cat)
        if not items:
            continue
        tag = cat.value
        lines = [f"<{tag}>", f"  <!-- {_CATEGORY_LABELS[cat]} -->"]
        lines.extend(f"  <item>{_escape_xml(item.content)}</item>" for item in items)
        lines.append(f"</{tag}>")
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return "<context>\n" + "\n".join(sections) + "\n</context>"


def _format_plain(memory: StructuredMemory) -> str:
    """Format memory as plain text (fallback / local models)."""
    sections: list[str] = []
    for cat in MemoryCategory:
        items = memory.get_category(cat)
        if not items:
            continue
        lines = [f"[{_CATEGORY_LABELS[cat]}]"]
        lines.extend(f"  {idx}. {item.content}" for idx, item in enumerate(items, 1))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


_FORMATTERS = {
    "markdown": _format_markdown,
    "xml": _format_xml,
    "plain": _format_plain,
}

# Model → preferred format
_MODEL_FORMAT_MAP = {
    "openai": "markdown",
    "gpt": "markdown",
    "chatgpt": "markdown",
    "claude": "xml",
    "anthropic": "xml",
    "local": "plain",
    "llama": "plain",
    "ollama": "plain",
}

_DISPLAY_NAMES = (
    ("claude", "Claude"),
    ("anthropic", "Claude"),
    ("openai", "ChatGPT"),
    ("gpt", "ChatGPT"),
    ("local", "your local model"),
    ("ollama", "your local model"),
    ("llama", "your local model"),
)


def resolve_format(target_model: str) -> str:
    """Map a model name to its preferred format."""
    model_lower = (target_model or "").lower()
    for key, fmt in _MODEL_FORMAT_MAP.items():
        if key in model_lower:
            return fmt
    return "markdown"


def target_display_name(target_model: str) -> str:
    lower = (target_model or "").lower()
    for key, name in _DISPLAY_NAMES:
        if key in lower:
            return name
    return target_model or "the target model"


# ---------------------------------------------------------------------------
# Prompt Builder
# ---------------------------------------------------------------------------


class PromptBuilder:
    """
    Reconstructs a system prompt from a ContextPackage for a target model.

    Usage::

        builder = PromptBuilder()
        system_prompt = builder.build(package, target_model="claude")
        paste_text = builder.paste_prompt(system_prompt, "claude")
    """

    PREAMBLE = (
        "You are continuing a conversation with a user. The following context "
        "was extracted from previous conversations and should be treated as "
        "established knowledge. Use this context to maintain continuity.\n\n"
    )

    def build(
        self,
        package: ContextPackage,
        target_model: str,
        *,
        include_preamble: bool = True,
        format_override: str | None = None,
        memory: StructuredMemory | None = None,
        footer_note: str = "",
    ) -> str:
        """
        Build a system prompt from a context package.

        Parameters
        ----------
        memory
            Render this memory instead of ``package.memory`` (e.g. a retrieval
            selection) while keeping the package's provenance footer.
        footer_note
            Extra text appended to the footer (e.g. retrieval statistics).
        """
        fmt = format_override or resolve_format(target_model)
        formatter = _FORMATTERS.get(fmt, _format_plain)
        chosen = memory if memory is not None else package.memory

        memory_text = formatter(chosen)
        if not memory_text.strip():
            logger.warning("Empty memory — generated prompt will have no context")
            return ""

        parts: list[str] = []
        if include_preamble:
            parts.append(self.PREAMBLE)
        parts.append(memory_text)

        footer = (
            f"\n\n---\n"
            f'Context: "{package.name}" v{package.version} | '
            f"Source: {package.source_model or 'unknown'} | "
            f"Items: {chosen.total_items}"
        )
        if memory is not None and chosen.total_items != package.memory.total_items:
            footer += f" of {package.memory.total_items}"
        if footer_note:
            footer += f" | {footer_note}"
        parts.append(footer)

        prompt = "\n".join(parts)
        logger.info("Built prompt for '%s' (%d chars, format=%s)", target_model, len(prompt), fmt)
        return prompt

    def build_from_retrieval(
        self,
        package: ContextPackage,
        result: RetrievalResult,
        target_model: str,
        *,
        format_override: str | None = None,
    ) -> str:
        """Render only the items a retrieval selected, with a note on how."""
        note = f"Selected by {result.method} retrieval for: {result.query!r}"
        return self.build(
            package,
            target_model,
            format_override=format_override,
            memory=result.to_memory(),
            footer_note=note,
        )

    def build_from_memory(
        self,
        memory: StructuredMemory,
        target_model: str,
        *,
        format_override: str | None = None,
    ) -> str:
        """Build a prompt directly from StructuredMemory (without a package)."""
        fmt = format_override or resolve_format(target_model)
        formatter = _FORMATTERS.get(fmt, _format_plain)
        return formatter(memory)

    @staticmethod
    def paste_prompt(
        context_block: str,
        target_model: str,
        *,
        extras: Iterable[tuple[str, str]] = (),
    ) -> str:
        """
        Wrap a context block in the paste-ready message used by the web UIs.

        Parameters
        ----------
        extras
            ``(title, body)`` sections appended after the context (e.g.
            attached files the user explicitly chose to include).
        """
        extras = list(extras)
        parts = [
            "I'm continuing a conversation from another AI assistant. "
            "Below is structured context extracted from our previous chats"
            + (", along with files I chose to include" if extras else "")
            + ". Please treat it as established knowledge and maintain continuity.",
            "",
            context_block,
        ]
        for title, body in extras:
            parts.extend(["", "=" * 60, title, "=" * 60, body])
        parts.extend(
            [
                "",
                "---",
                "Please confirm you've understood this context, then I'll continue "
                "with my questions.",
            ]
        )
        return "\n".join(parts)

    @staticmethod
    def _resolve_format(target_model: str) -> str:  # backward compatibility
        return resolve_format(target_model)
