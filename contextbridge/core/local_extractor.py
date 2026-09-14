"""
Local rule-based memory extractor — works entirely offline, no API key needed.

Uses keyword matching, pattern recognition, and heuristics to extract
structured memory from chat transcripts. Less accurate than LLM-based
extraction, but requires zero external dependencies.
"""

from __future__ import annotations

import re

from contextbridge.models import MemoryCategory, MemoryItem, StructuredMemory

# ---------------------------------------------------------------------------
# Keyword & pattern dictionaries
# ---------------------------------------------------------------------------

IDENTITY_PATTERNS = [
    r"(?:my name is|i'?m|i am|call me)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
    r"(?:i work (?:at|for|as)|i'?m a|my role is|my job is|i am a)\s+(.+?)(?:\.|,|$)",
    r"(?:i'?m studying|i study|i'?m a student|my major is|i'?m majoring in)\s+(.+?)(?:\.|,|$)",
    r"(?:my background is|i have experience in|i specialize in)\s+(.+?)(?:\.|,|$)",
]

PROJECT_PATTERNS = [
    r"(?:i'?m building|i'?m working on|i'?m developing|my project is|the project is|building a|creating a)\s+(.+?)(?:\.|,|$)",  # noqa: E501
    r"(?:i(?:'m| am) using|tech stack|using|built with|powered by)\s+((?:[A-Z][a-zA-Z]*(?:[,\s]+(?:and\s+)?)?)+)(?:\.|,|$)",  # noqa: E501
    r"(?:the app|the application|the tool|the system|the platform)\s+(?:is |called |named )?(.+?)(?:\.|,|$)",  # noqa: E501
]

DECISION_KEYWORDS = [
    "i decided",
    "i chose",
    "i went with",
    "i picked",
    "we decided",
    "the decision is",
    "i'll use",
    "i'll go with",
    "let's use",
    "i want to use",
    "we should use",
    "going with",
]

TASK_KEYWORDS = [
    "todo",
    "to-do",
    "next step",
    "need to",
    "have to",
    "should",
    "plan to",
    "will implement",
    "will build",
    "will add",
    "still need",
    "remaining",
    "pending",
    "haven't done",
    "not yet",
    "left to do",
]

PREFERENCE_KEYWORDS = [
    "i prefer",
    "i like",
    "i want",
    "i always",
    "my style",
    "i usually",
    "i tend to",
    "my preference",
    "i favor",
    "i don't like",
    "i avoid",
    "i never",
    "i hate",
]

FACT_KEYWORDS = [
    "the constraint is",
    "the requirement is",
    "must be",
    "has to be",
    "the limit is",
    "the deadline is",
    "the budget is",
    "important to note",
    "keep in mind",
    "note that",
    "the rule is",
    "make sure",
]


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------


class LocalExtractor:
    """Extract structured memory from text using rules — no API needed."""

    def extract(self, text: str, *, origin: str = "") -> StructuredMemory:
        """Extract structured memory from raw text.

        Args:
            text: Raw chat transcript or document text.
            origin: Provenance label stamped on every extracted item.

        Returns:
            StructuredMemory with items categorised via heuristics.
        """
        # Split into sentences for analysis
        sentences = self._split_sentences(text)

        # Extract user messages (they contain the important context)
        user_sentences = self._extract_user_sentences(text)
        all_sentences = user_sentences if user_sentences else sentences

        memory = StructuredMemory()

        memory.identity = self._extract_identity(all_sentences)
        memory.projects = self._extract_projects(all_sentences)
        memory.decisions = self._extract_decisions(all_sentences)
        memory.open_tasks = self._extract_tasks(all_sentences)
        memory.preferences = self._extract_preferences(all_sentences)
        memory.facts = self._extract_facts(all_sentences)

        # Deduplicate within each category
        memory = self._deduplicate(memory)

        if origin:
            for item in memory.all_items:
                item.origin = origin

        return memory

    # -----------------------------------------------------------------------
    # Category extractors
    # -----------------------------------------------------------------------

    def _extract_identity(self, sentences: list[str]) -> list[MemoryItem]:
        items = []
        for sent in sentences:
            for pattern in IDENTITY_PATTERNS:
                match = re.search(pattern, sent, re.IGNORECASE)
                if match:
                    content = self._clean(sent)
                    items.append(
                        MemoryItem(
                            category=MemoryCategory.IDENTITY,
                            content=content,
                            confidence=0.8,
                            source="rule:identity_pattern",
                        )
                    )
                    break
        return items

    def _extract_projects(self, sentences: list[str]) -> list[MemoryItem]:
        items = []
        for sent in sentences:
            for pattern in PROJECT_PATTERNS:
                match = re.search(pattern, sent, re.IGNORECASE)
                if match:
                    content = self._clean(sent)
                    items.append(
                        MemoryItem(
                            category=MemoryCategory.PROJECTS,
                            content=content,
                            confidence=0.75,
                            source="rule:project_pattern",
                        )
                    )
                    break
        return items

    def _extract_decisions(self, sentences: list[str]) -> list[MemoryItem]:
        items = []
        for sent in sentences:
            sent_lower = sent.lower()
            if any(kw in sent_lower for kw in DECISION_KEYWORDS):
                items.append(
                    MemoryItem(
                        category=MemoryCategory.DECISIONS,
                        content=self._clean(sent),
                        confidence=0.7,
                        source="rule:decision_keyword",
                    )
                )
        return items

    def _extract_tasks(self, sentences: list[str]) -> list[MemoryItem]:
        items = []
        for sent in sentences:
            sent_lower = sent.lower()
            if any(kw in sent_lower for kw in TASK_KEYWORDS):
                # Skip if it's a completed task
                if any(w in sent_lower for w in ["done", "finished", "completed", "already"]):
                    continue
                items.append(
                    MemoryItem(
                        category=MemoryCategory.OPEN_TASKS,
                        content=self._clean(sent),
                        confidence=0.7,
                        source="rule:task_keyword",
                    )
                )
        return items

    def _extract_preferences(self, sentences: list[str]) -> list[MemoryItem]:
        items = []
        for sent in sentences:
            sent_lower = sent.lower()
            if any(kw in sent_lower for kw in PREFERENCE_KEYWORDS):
                items.append(
                    MemoryItem(
                        category=MemoryCategory.PREFERENCES,
                        content=self._clean(sent),
                        confidence=0.7,
                        source="rule:preference_keyword",
                    )
                )
        return items

    def _extract_facts(self, sentences: list[str]) -> list[MemoryItem]:
        items = []
        for sent in sentences:
            sent_lower = sent.lower()
            if any(kw in sent_lower for kw in FACT_KEYWORDS):
                items.append(
                    MemoryItem(
                        category=MemoryCategory.FACTS,
                        content=self._clean(sent),
                        confidence=0.65,
                        source="rule:fact_keyword",
                    )
                )
        return items

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _split_sentences(self, text: str) -> list[str]:
        """Split text into sentences."""
        # Split on periods, question marks, exclamation points, newlines
        parts = re.split(r"(?<=[.!?])\s+|\n+", text)
        return [p.strip() for p in parts if p.strip() and len(p.strip()) > 10]

    def _extract_user_sentences(self, text: str) -> list[str]:
        """Extract only user messages from a chat transcript."""
        user_msgs = []
        # Match "User: ..." or "Human: ..." patterns
        pattern = r"(?:^|\n)\s*(?:User|Human|You|Me)\s*:\s*(.+?)(?=\n\s*(?:Assistant|AI|Claude|ChatGPT|System)\s*:|$)"  # noqa: E501
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        for msg in matches:
            user_msgs.extend(self._split_sentences(msg))
        return user_msgs

    def _clean(self, text: str) -> str:
        """Clean a sentence for storage."""
        text = text.strip()
        # Remove leading "User: " etc
        text = re.sub(r"^(?:User|Human|You|Me)\s*:\s*", "", text, flags=re.IGNORECASE)
        # Truncate long sentences
        if len(text) > 300:
            text = text[:297] + "..."
        return text

    def _deduplicate(self, memory: StructuredMemory) -> StructuredMemory:
        """Remove duplicate items within each category."""
        for cat in MemoryCategory:
            items = memory.get_category(cat)
            seen = set()
            unique = []
            for item in items:
                key = item.content.lower().strip()
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            setattr(memory, cat.value, unique)
        return memory
