"""
Prompt Builder — reconstructs system prompts from context packages.

Adapts the output format for the target model's conventions
(e.g. XML tags for Claude, markdown headers for GPT).
"""

from __future__ import annotations

import logging

from contextbridge.models import (
    ContextPackage,
    MemoryCategory,
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
        label = _CATEGORY_LABELS[cat]
        lines = [f"## {label}"]
        for item in items:
            lines.append(f"- {item.content}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _format_xml(memory: StructuredMemory) -> str:
    """Format memory as XML tags (preferred by Claude)."""
    sections: list[str] = []
    for cat in MemoryCategory:
        items = memory.get_category(cat)
        if not items:
            continue
        label = _CATEGORY_LABELS[cat]
        tag = cat.value
        lines = [f"<{tag}>"]
        lines.append(f"  <!-- {label} -->")
        for item in items:
            lines.append(f"  <item>{item.content}</item>")
        lines.append(f"</{tag}>")
        sections.append("\n".join(lines))

    return "<context>\n" + "\n".join(sections) + "\n</context>"


def _format_plain(memory: StructuredMemory) -> str:
    """Format memory as plain text (fallback)."""
    sections: list[str] = []
    for cat in MemoryCategory:
        items = memory.get_category(cat)
        if not items:
            continue
        label = _CATEGORY_LABELS[cat]
        lines = [f"[{label}]"]
        for idx, item in enumerate(items, 1):
            lines.append(f"  {idx}. {item.content}")
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
    "claude": "xml",
    "anthropic": "xml",
    "local": "plain",
    "llama": "plain",
    "ollama": "plain",
}


# ---------------------------------------------------------------------------
# Prompt Builder
# ---------------------------------------------------------------------------


class PromptBuilder:
    """
    Reconstructs a system prompt from a ContextPackage for a target model.

    The builder selects the appropriate formatting convention based on
    the target model and injects structured memory as persistent context.

    Usage::

        builder = PromptBuilder()
        system_prompt = builder.build(package, target_model="claude")
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
    ) -> str:
        """
        Build a system prompt from a context package.

        Parameters
        ----------
        package : ContextPackage
            The context package containing structured memory.
        target_model : str
            The target model identifier (e.g. 'openai', 'claude', 'local').
        include_preamble : bool, optional
            Whether to include the introductory preamble.
        format_override : str, optional
            Force a specific format ('markdown', 'xml', 'plain').

        Returns
        -------
        str
            A complete system prompt ready for injection.
        """
        fmt = format_override or self._resolve_format(target_model)
        formatter = _FORMATTERS.get(fmt, _format_plain)

        memory_text = formatter(package.memory)
        if not memory_text.strip():
            logger.warning("Empty memory — generated prompt will have no context")
            return ""

        parts: list[str] = []
        if include_preamble:
            parts.append(self.PREAMBLE)

        parts.append(memory_text)

        # Append metadata footer
        parts.append(
            f"\n\n---\n"
            f'Context: "{package.name}" v{package.version} | '
            f"Source: {package.source_model or 'unknown'} | "
            f"Items: {package.memory.total_items}"
        )

        prompt = "\n".join(parts)
        logger.info(
            "Built prompt for '%s' (%d chars, format=%s)",
            target_model,
            len(prompt),
            fmt,
        )
        return prompt

    def build_from_memory(
        self,
        memory: StructuredMemory,
        target_model: str,
        *,
        format_override: str | None = None,
    ) -> str:
        """
        Build a prompt directly from StructuredMemory (without a package).

        Convenience method for quick one-off prompts.
        """
        fmt = format_override or self._resolve_format(target_model)
        formatter = _FORMATTERS.get(fmt, _format_plain)
        return formatter(memory)

    @staticmethod
    def _resolve_format(target_model: str) -> str:
        """Map a model name to its preferred format."""
        model_lower = target_model.lower()
        for key, fmt in _MODEL_FORMAT_MAP.items():
            if key in model_lower:
                return fmt
        return "markdown"  # default
