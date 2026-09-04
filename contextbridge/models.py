"""Pydantic models for ContextBridge data structures."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Memory Categories
# ---------------------------------------------------------------------------


class MemoryCategory(StrEnum):
    """The six structured memory categories extracted from conversations."""

    IDENTITY = "identity"
    PROJECTS = "projects"
    FACTS = "facts"
    DECISIONS = "decisions"
    OPEN_TASKS = "open_tasks"
    PREFERENCES = "preferences"


class MemoryItem(BaseModel):
    """A single piece of extracted memory."""

    category: MemoryCategory
    content: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = Field(default="", description="Origin context or quote")
    embedding: list[float] | None = Field(default=None, exclude=True)


class StructuredMemory(BaseModel):
    """Complete structured memory extracted from a conversation."""

    identity: list[MemoryItem] = Field(default_factory=list)
    projects: list[MemoryItem] = Field(default_factory=list)
    facts: list[MemoryItem] = Field(default_factory=list)
    decisions: list[MemoryItem] = Field(default_factory=list)
    open_tasks: list[MemoryItem] = Field(default_factory=list)
    preferences: list[MemoryItem] = Field(default_factory=list)

    @property
    def all_items(self) -> list[MemoryItem]:
        """Flatten all memory items across categories."""
        return (
            self.identity
            + self.projects
            + self.facts
            + self.decisions
            + self.open_tasks
            + self.preferences
        )

    @property
    def total_items(self) -> int:
        return len(self.all_items)

    def get_category(self, category: MemoryCategory) -> list[MemoryItem]:
        """Get items for a specific category."""
        return getattr(self, category.value, [])

    def merge(self, other: StructuredMemory) -> StructuredMemory:
        """Merge another StructuredMemory into this one, deduplicating by content."""
        merged = StructuredMemory()
        for cat in MemoryCategory:
            existing = {item.content for item in self.get_category(cat)}
            combined = list(self.get_category(cat))
            for item in other.get_category(cat):
                if item.content not in existing:
                    combined.append(item)
                    existing.add(item.content)
            setattr(merged, cat.value, combined)
        return merged


# ---------------------------------------------------------------------------
# Context Package (versioned, portable)
# ---------------------------------------------------------------------------


class ContextDiff(BaseModel):
    """Diff between two context package versions."""

    added: list[MemoryItem] = Field(default_factory=list)
    removed: list[MemoryItem] = Field(default_factory=list)
    modified: list[dict[str, Any]] = Field(default_factory=list)


class ContextVersion(BaseModel):
    """A snapshot in the version history."""

    version: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diff: ContextDiff = Field(default_factory=ContextDiff)
    source_model: str = ""


class ContextPackage(BaseModel):
    """A versioned, portable context package — the core data artifact."""

    name: str
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_model: str = ""
    memory: StructuredMemory = Field(default_factory=StructuredMemory)
    history: list[ContextVersion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def bump_version(self, diff: ContextDiff, source_model: str = "") -> None:
        """Increment version and record the diff in history."""
        snapshot = ContextVersion(
            version=self.version,
            diff=diff,
            source_model=self.source_model,
        )
        self.history.append(snapshot)
        self.version += 1
        self.updated_at = datetime.now(UTC)
        if source_model:
            self.source_model = source_model


# ---------------------------------------------------------------------------
# Chat transcript helpers
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """A single message in a conversation transcript."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime | None = None


class ChatTranscript(BaseModel):
    """A parsed conversation transcript."""

    messages: list[ChatMessage] = Field(default_factory=list)
    source_model: str = ""
    raw_text: str = ""

    @property
    def total_tokens_estimate(self) -> int:
        """Rough token estimate (1 token ≈ 4 chars)."""
        return sum(len(m.content) for m in self.messages) // 4
