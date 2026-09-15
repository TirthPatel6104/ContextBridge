"""Pydantic models for ContextBridge data structures.

Every persisted artefact (memory items, packages, version history) is defined
here so that storage backends, the CLI, the web app, and the evaluation suite
share one schema.  New fields are always given defaults so that packages
written by earlier versions keep validating.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

#: Bumped whenever the on-disk package shape changes in a way that matters
#: for readers.  Version 1 packages (no item ids / origins) still load, and so
#: do version 2 packages (no sharing policy / lifecycle fields).
SCHEMA_VERSION = 3

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


CATEGORY_LABELS: dict[MemoryCategory, str] = {
    MemoryCategory.IDENTITY: "Identity",
    MemoryCategory.PROJECTS: "Projects",
    MemoryCategory.FACTS: "Facts",
    MemoryCategory.DECISIONS: "Decisions",
    MemoryCategory.OPEN_TASKS: "Open tasks",
    MemoryCategory.PREFERENCES: "Preferences",
}


def make_item_id(category: str, content: str) -> str:
    """Deterministic short id derived from category + normalised content.

    Two extractions of the same statement produce the same id, which lets
    diffs, merges, and review decisions refer to items stably.
    """
    normalised = " ".join(content.split()).strip().lower()
    digest = hashlib.sha1(f"{category}\x1f{normalised}".encode()).hexdigest()
    return digest[:12]


class RedactionRecord(BaseModel):
    """What was masked inside an item, without storing the masked value."""

    kind: str = Field(description="Detector name, e.g. 'email' or 'api_key'")
    count: int = Field(default=1, ge=1)


class SharingPolicy(StrEnum):
    """Who an item may be shown to when a prompt is built.

    * ``any``        – may go to any target (default).
    * ``local_only`` – only rendered for local models (Ollama); withheld from
                       ChatGPT / Claude and any other cloud target.
    * ``never``      – kept for your own reference, never rendered in a prompt.
    """

    ANY = "any"
    LOCAL_ONLY = "local_only"
    NEVER = "never"


class ItemStatus(StrEnum):
    """Lifecycle state of a memory item.

    Only ``active`` items are retrieved or rendered.  ``superseded`` items are
    kept for history and point at the item that replaced them; ``done`` marks a
    completed open task.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DONE = "done"


#: Item fields whose changes are recorded in the version history so that a
#: rollback restores them.  Content changes produce a new id and therefore show
#: up as add/remove instead.
TRACKED_ITEM_FIELDS: tuple[str, ...] = ("status", "superseded_by", "sharing")


class MemoryItem(BaseModel):
    """A single piece of extracted memory."""

    id: str = Field(default="", description="Stable content-derived identifier")
    category: MemoryCategory
    content: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = Field(default="", description="Origin context or quote")
    origin: str = Field(
        default="",
        description="Where the item came from: file name, conversation title, or package",
    )
    redactions: list[RedactionRecord] = Field(default_factory=list)
    sharing: SharingPolicy = Field(
        default=SharingPolicy.ANY, description="Which targets may see this item in a prompt"
    )
    status: ItemStatus = Field(default=ItemStatus.ACTIVE)
    superseded_by: str = Field(default="", description="Id of the item that replaced this one")
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    seen_count: int = Field(
        default=1, ge=1, description="How many imports have produced this statement"
    )
    embedding: list[float] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _ensure_id(self) -> MemoryItem:
        if not self.id:
            self.id = make_item_id(self.category.value, self.content)
        return self

    @property
    def was_redacted(self) -> bool:
        return bool(self.redactions)

    @property
    def is_active(self) -> bool:
        return self.status == ItemStatus.ACTIVE

    def tracked_fields(self) -> dict[str, str]:
        """The versioned lifecycle / sharing fields as plain strings."""
        return {name: str(getattr(self, name)) for name in TRACKED_ITEM_FIELDS}


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
        """Flatten all memory items across categories (category order)."""
        return [item for cat in MemoryCategory for item in self.get_category(cat)]

    @property
    def total_items(self) -> int:
        return len(self.all_items)

    def get_category(self, category: MemoryCategory) -> list[MemoryItem]:
        """Get items for a specific category."""
        return getattr(self, category.value, [])

    def set_category(self, category: MemoryCategory, items: list[MemoryItem]) -> None:
        setattr(self, category.value, list(items))

    def counts(self) -> dict[str, int]:
        return {cat.value: len(self.get_category(cat)) for cat in MemoryCategory}

    def items_by_id(self) -> dict[str, MemoryItem]:
        return {item.id: item for item in self.all_items}

    def filter_categories(self, categories: list[MemoryCategory] | None) -> StructuredMemory:
        """Return a copy containing only the given categories (all if None)."""
        if not categories:
            return self.model_copy(deep=True)
        keep = set(categories)
        filtered = StructuredMemory()
        for cat in MemoryCategory:
            if cat in keep:
                filtered.set_category(cat, [i.model_copy() for i in self.get_category(cat)])
        return filtered

    def without_ids(self, ids: set[str] | list[str]) -> StructuredMemory:
        """Return a copy with the given item ids removed."""
        drop = set(ids)
        result = StructuredMemory()
        for cat in MemoryCategory:
            result.set_category(cat, [i for i in self.get_category(cat) if i.id not in drop])
        return result

    def select(self, predicate: Callable[[MemoryItem], bool]) -> StructuredMemory:
        """Return a copy containing only the items for which *predicate* is true."""
        result = StructuredMemory()
        for cat in MemoryCategory:
            result.set_category(cat, [i for i in self.get_category(cat) if predicate(i)])
        return result

    def active(self) -> StructuredMemory:
        """Only items that are still current (not superseded, not done)."""
        return self.select(lambda i: i.is_active)

    def status_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ItemStatus}
        for item in self.all_items:
            counts[item.status.value] += 1
        return counts

    @classmethod
    def from_items(cls, items: list[MemoryItem]) -> StructuredMemory:
        memory = cls()
        for item in items:
            memory.get_category(item.category).append(item)
        return memory

    def merge(self, other: StructuredMemory) -> StructuredMemory:
        """Merge another StructuredMemory into this one, deduplicating by exact content.

        For fuzzy duplicate and conflict detection use
        :class:`contextbridge.core.merger.PackageMerger`.
        """
        merged = StructuredMemory()
        for cat in MemoryCategory:
            existing = {item.content for item in self.get_category(cat)}
            combined = list(self.get_category(cat))
            for item in other.get_category(cat):
                if item.content not in existing:
                    combined.append(item)
                    existing.add(item.content)
            merged.set_category(cat, combined)
        return merged


# ---------------------------------------------------------------------------
# Context Package (versioned, portable)
# ---------------------------------------------------------------------------


class ContextDiff(BaseModel):
    """Diff between two context package versions.

    ``modified`` entries describe lifecycle / sharing changes on items that
    kept their id: ``{"id", "category", "before": {...}, "after": {...}}`` with
    the :data:`TRACKED_ITEM_FIELDS` values on both sides.
    """

    added: list[MemoryItem] = Field(default_factory=list)
    removed: list[MemoryItem] = Field(default_factory=list)
    modified: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


class ContextVersion(BaseModel):
    """A snapshot in the version history."""

    version: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diff: ContextDiff = Field(default_factory=ContextDiff)
    source_model: str = ""
    note: str = ""


class ContextPackage(BaseModel):
    """A versioned, portable context package — the core data artifact."""

    name: str
    schema_version: int = SCHEMA_VERSION
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_model: str = ""
    memory: StructuredMemory = Field(default_factory=StructuredMemory)
    history: list[ContextVersion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def bump_version(self, diff: ContextDiff, source_model: str = "", note: str = "") -> None:
        """Increment version and record the diff that produced the new version."""
        self.version += 1
        self.updated_at = datetime.now(UTC)
        if source_model:
            self.source_model = source_model
        self.history.append(
            ContextVersion(
                version=self.version,
                diff=diff,
                source_model=self.source_model,
                note=note,
            )
        )


class PackageEnvelope(BaseModel):
    """Portable wrapper used for package export / import files."""

    format: str = "contextbridge-package"
    schema_version: int = SCHEMA_VERSION
    exported_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    generator: str = "contextbridge"
    package: ContextPackage


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class RetrievalOptions(BaseModel):
    """User-tunable knobs for memory retrieval."""

    top_k: int = Field(default=5, ge=1, le=100)
    categories: list[MemoryCategory] | None = Field(
        default=None, description="Restrict to these categories (None = all)"
    )
    token_budget: int | None = Field(
        default=None, ge=1, description="Stop selecting once this many estimated tokens are used"
    )
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    include_inactive: bool = Field(
        default=False, description="Also consider superseded / done items (off by default)"
    )


class ScoredItem(BaseModel):
    """A memory item together with the evidence for why it was selected."""

    item: MemoryItem
    rank: int
    score: float = Field(ge=0.0, le=1.0)
    lexical_score: float = Field(default=0.0, ge=0.0, le=1.0)
    embedding_score: float | None = None
    matched_terms: list[str] = Field(default_factory=list)
    reason: str = ""
    tokens: int = 0


class RetrievalResult(BaseModel):
    """Everything the UI needs to explain a retrieval decision."""

    query: str
    method: str = Field(description="'lexical' or 'hybrid' (lexical + embeddings)")
    options: RetrievalOptions
    selected: list[ScoredItem] = Field(default_factory=list)
    total_items: int = 0
    considered: int = 0
    excluded_inactive: int = 0
    dropped_below_min_score: int = 0
    dropped_for_budget: int = 0
    tokens_selected: int = 0
    tokens_full_memory: int = 0
    tokens_transcript: int | None = None

    @property
    def tokens_saved_vs_full_memory(self) -> int:
        return max(0, self.tokens_full_memory - self.tokens_selected)

    @property
    def tokens_saved_vs_transcript(self) -> int | None:
        if self.tokens_transcript is None:
            return None
        return max(0, self.tokens_transcript - self.tokens_selected)

    def to_memory(self) -> StructuredMemory:
        """Selected items as a StructuredMemory, preserving category order."""
        return StructuredMemory.from_items([s.item for s in self.selected])

    def summary(self) -> dict[str, Any]:
        return {
            "selected": len(self.selected),
            "considered": self.considered,
            "total_items": self.total_items,
            "method": self.method,
            "tokens_selected": self.tokens_selected,
            "tokens_full_memory": self.tokens_full_memory,
            "tokens_saved_vs_full_memory": self.tokens_saved_vs_full_memory,
            "tokens_transcript": self.tokens_transcript,
            "tokens_saved_vs_transcript": self.tokens_saved_vs_transcript,
            "dropped_below_min_score": self.dropped_below_min_score,
            "dropped_for_budget": self.dropped_for_budget,
            "excluded_inactive": self.excluded_inactive,
        }


# ---------------------------------------------------------------------------
# Sharing boundaries & egress ledger
# ---------------------------------------------------------------------------


class WithheldItem(BaseModel):
    """An item that was deliberately left out of a prompt, and why."""

    item: MemoryItem
    reason: str


class EgressRecord(BaseModel):
    """One entry in the egress ledger: what was rendered for which target.

    The ledger stores item *ids* and counts, never the rendered text, so the
    audit trail itself does not become a second copy of your memory.
    """

    id: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    package_name: str
    package_version: int
    target_model: str
    target_kind: str = Field(description="'cloud' or 'local'")
    surface: str = Field(default="cli", description="cli, dashboard, extension, api, query")
    query: str = ""
    item_ids: list[str] = Field(default_factory=list)
    item_count: int = 0
    withheld_count: int = 0
    tokens: int = 0


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
        """Rough token estimate for the message bodies (see core.tokens)."""
        from contextbridge.core.tokens import estimate_tokens

        return sum(estimate_tokens(m.content) for m in self.messages)
