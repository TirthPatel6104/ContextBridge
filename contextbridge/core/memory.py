"""
Memory Extractor — converts raw conversations into structured memory.

Uses an LLM to intelligently decompose a conversation into six categories:
Identity, Projects, Facts, Decisions, Open Tasks, and Preferences.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from contextbridge.models import (
    ChatMessage,
    ChatTranscript,
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)

if TYPE_CHECKING:
    from contextbridge.core.llm_interface import LLMInterface

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """\
You are a context extraction engine. Your job is to read a conversation \
transcript and extract structured memory into exactly six categories.

Return a JSON object with these keys (each is a list of objects):
- "identity": Who the user is — name, role, background, expertise.
- "projects": Active projects, tech stacks, goals, timelines.
- "facts": Established facts, constraints, requirements.
- "decisions": Architectural or design decisions that were made.
- "open_tasks": Pending items, TODOs, next steps.
- "preferences": Code style, communication preferences, tool preferences.

Each item in a list must have:
- "content": A concise statement of the memory (1-2 sentences).
- "confidence": A float 0.0-1.0 indicating how confident you are.
- "source": A brief quote or reference from the conversation.

Rules:
- Be exhaustive — extract everything relevant.
- Be concise — each item should be a self-contained fact.
- Do NOT fabricate information not present in the conversation.
- If a category has no items, return an empty list.
- Return ONLY valid JSON, no markdown fences, no explanation.
"""


# ---------------------------------------------------------------------------
# Chat parser
# ---------------------------------------------------------------------------


def parse_chat_transcript(raw_text: str, source_model: str = "") -> ChatTranscript:
    """
    Parse a raw chat transcript into structured messages.

    Supports common formats:
    - "User: ...", "Assistant: ..."
    - "Human: ...", "AI: ..."
    - JSON arrays of {role, content}
    """
    messages: list[ChatMessage] = []

    # Try JSON first
    try:
        data = json.loads(raw_text)
        if isinstance(data, list):
            for msg in data:
                messages.append(
                    ChatMessage(
                        role=msg.get("role", "user"),
                        content=msg.get("content", ""),
                    )
                )
            return ChatTranscript(messages=messages, source_model=source_model, raw_text=raw_text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Regex-based parsing for plain text transcripts
    pattern = re.compile(
        r"^(User|Human|Assistant|AI|System)\s*:\s*",
        re.IGNORECASE | re.MULTILINE,
    )
    splits = pattern.split(raw_text)

    # splits looks like: [preamble, role1, content1, role2, content2, ...]
    if len(splits) >= 3:
        i = 1  # skip preamble
        while i < len(splits) - 1:
            role_raw = splits[i].strip().lower()
            content = splits[i + 1].strip()
            role = _normalize_role(role_raw)
            if content:
                messages.append(ChatMessage(role=role, content=content))
            i += 2
    else:
        # Fallback: treat entire text as a single user message
        messages.append(ChatMessage(role="user", content=raw_text.strip()))

    return ChatTranscript(messages=messages, source_model=source_model, raw_text=raw_text)


def _normalize_role(role: str) -> str:
    """Map various role labels to standard roles."""
    role = role.lower().strip()
    if role in ("user", "human"):
        return "user"
    if role in ("assistant", "ai", "bot"):
        return "assistant"
    if role in ("system",):
        return "system"
    return "user"


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class MemoryExtractor:
    """
    Extracts structured memory from a conversation using an LLM adapter.

    Usage::

        extractor = MemoryExtractor()
        memory = await extractor.extract(raw_chat, adapter)
    """

    def __init__(self, system_prompt: str | None = None) -> None:
        self._system_prompt = system_prompt or EXTRACTION_SYSTEM_PROMPT

    async def extract(
        self,
        raw_chat: str,
        adapter: LLMInterface,
        *,
        source_model: str = "",
        origin: str = "",
    ) -> StructuredMemory:
        """
        Extract structured memory from a raw chat transcript.

        Parameters
        ----------
        raw_chat : str
            The raw conversation text (any supported format).
        adapter : LLMInterface
            The LLM adapter to use for extraction.
        source_model : str, optional
            Name of the model that produced the conversation.
        origin : str, optional
            Provenance label (file name, chat title) stamped on every item.

        Returns
        -------
        StructuredMemory
            The extracted and structured memory.
        """
        transcript = parse_chat_transcript(raw_chat, source_model)
        logger.info(
            "Extracting memory from %d messages (~%d tokens)",
            len(transcript.messages),
            transcript.total_tokens_estimate,
        )

        # Build the extraction prompt
        conversation_text = self._format_transcript(transcript)
        prompt = (
            f"Extract structured memory from the following conversation:\n\n{conversation_text}"
        )

        # Call the LLM
        raw_response = await adapter.send(
            prompt,
            system_prompt=self._system_prompt,
            temperature=0.1,  # low temperature for structured output
        )

        # Parse the response
        memory = self._parse_response(raw_response, origin=origin)
        logger.info("Extracted %d total memory items", memory.total_items)
        return memory

    async def extract_incremental(
        self,
        raw_chat: str,
        existing: StructuredMemory,
        adapter: LLMInterface,
        *,
        source_model: str = "",
        origin: str = "",
    ) -> StructuredMemory:
        """
        Extract new memory from a conversation, merging with existing memory.

        This avoids duplicating facts already captured in previous extractions.
        """
        new_memory = await self.extract(raw_chat, adapter, source_model=source_model, origin=origin)
        return existing.merge(new_memory)

    # -- Private helpers ----------------------------------------------------

    @staticmethod
    def _format_transcript(transcript: ChatTranscript) -> str:
        """Format a transcript into a readable string for the LLM."""
        lines: list[str] = []
        for msg in transcript.messages:
            role_label = msg.role.capitalize()
            lines.append(f"{role_label}: {msg.content}")
        return "\n\n".join(lines)

    @staticmethod
    def _parse_response(raw: str, *, origin: str = "") -> StructuredMemory:
        """Parse the LLM's JSON response into a StructuredMemory object.

        The raw response is never logged: it contains the user's memory.
        """
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse LLM response as JSON (%s)", exc.__class__.__name__)
            return StructuredMemory()
        if not isinstance(data, dict):
            logger.error("LLM response was valid JSON but not an object")
            return StructuredMemory()

        memory = StructuredMemory()
        for cat in MemoryCategory:
            items_data = data.get(cat.value, [])
            if not isinstance(items_data, list):
                continue
            items: list[MemoryItem] = []
            for item_data in items_data:
                if isinstance(item_data, str) and item_data.strip():
                    items.append(MemoryItem(category=cat, content=item_data.strip(), origin=origin))
                elif isinstance(item_data, dict):
                    content = str(item_data.get("content", "")).strip()
                    if not content:
                        continue
                    try:
                        confidence = float(item_data.get("confidence", 1.0))
                    except (TypeError, ValueError):
                        confidence = 1.0
                    items.append(
                        MemoryItem(
                            category=cat,
                            content=content,
                            confidence=min(1.0, max(0.0, confidence)),
                            source=str(item_data.get("source", "") or ""),
                            origin=origin,
                        )
                    )
            memory.set_category(cat, items)

        return memory
