"""
Memory lifecycle — supersession, completed tasks, hand-off detection, health.

Memory goes stale.  A decision is reversed in a later chat, an open task gets
finished, and the same fact keeps being re-stated.  Vendor memories overwrite
silently; ContextBridge keeps the old item, marks it *superseded* with a
pointer to its replacement, and records how often and how recently each item
was seen so you can tell a well-corroborated fact from a one-off remark.

This module also recognises transcripts that *started* from a ContextBridge
prompt (a "hand-off").  The pasted context block is stripped before
extraction so the package does not re-import an echo of itself, and the link
back to the source package and version is recorded.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from contextbridge.core.merger import Conflict, PackageMerger
from contextbridge.models import (
    ContextPackage,
    EgressRecord,
    ItemStatus,
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)

# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------


def set_status(
    memory: StructuredMemory, item_ids: set[str] | list[str], status: ItemStatus
) -> tuple[StructuredMemory, int]:
    """Return a copy of *memory* with *status* applied to the given ids, plus the change count."""
    targets = set(item_ids)
    changed = 0
    updated = StructuredMemory()
    for item in memory.all_items:
        if item.id in targets and item.status != status:
            patch: dict[str, Any] = {"status": status}
            if status == ItemStatus.ACTIVE:
                patch["superseded_by"] = ""
            item = item.model_copy(update=patch)
            changed += 1
        updated.get_category(item.category).append(item)
    return updated, changed


def supersede(memory: StructuredMemory, old_id: str, new_id: str) -> StructuredMemory:
    """Mark *old_id* as superseded by *new_id* (both must exist and differ).

    Raises ``KeyError`` when an id is unknown and ``ValueError`` when the ids
    are the same or *new_id* is itself superseded (no chains that end nowhere).
    """
    by_id = memory.items_by_id()
    if old_id not in by_id:
        raise KeyError(old_id)
    if new_id not in by_id:
        raise KeyError(new_id)
    if old_id == new_id:
        raise ValueError("An item cannot supersede itself")
    if by_id[new_id].status == ItemStatus.SUPERSEDED:
        raise ValueError("The replacement item is itself superseded; pick its replacement")
    updated = StructuredMemory()
    for item in memory.all_items:
        if item.id == old_id:
            item = item.model_copy(
                update={"status": ItemStatus.SUPERSEDED, "superseded_by": new_id}
            )
        updated.get_category(item.category).append(item)
    return updated


# ---------------------------------------------------------------------------
# Corroboration: the same statement seen again
# ---------------------------------------------------------------------------


def absorb(
    existing: StructuredMemory, incoming: StructuredMemory, *, now: datetime | None = None
) -> tuple[StructuredMemory, int, int]:
    """Merge *incoming* into *existing* by id, tracking sightings.

    Items already present get ``last_seen`` bumped, ``seen_count`` incremented,
    and a new origin appended.  Their status and sharing policy are kept: a
    superseded decision that is restated in an old export stays superseded.
    Returns ``(memory, added, re_seen)``.
    """
    from contextbridge.core.merger import ORIGIN_SEPARATOR, _merge_origins

    now = now or datetime.now(UTC)
    by_id = existing.items_by_id()
    merged = StructuredMemory()
    added = re_seen = 0
    updates: dict[str, MemoryItem] = {}
    new_items: list[MemoryItem] = []
    for item in incoming.all_items:
        current = by_id.get(item.id)
        if current is None:
            new_items.append(item)
            added += 1
            continue
        origins = _merge_origins([current.origin, item.origin])
        updates[item.id] = current.model_copy(
            update={
                "last_seen": now,
                "seen_count": current.seen_count + 1,
                "origin": ORIGIN_SEPARATOR.join(origins),
                # Prefer the richer excerpt when the stored one is empty.
                "source": current.source or item.source,
            }
        )
        re_seen += 1
    for cat in MemoryCategory:
        items = [updates.get(i.id, i) for i in existing.get_category(cat)]
        items.extend(i for i in new_items if i.category == cat)
        merged.set_category(cat, items)
    return merged, added, re_seen


# ---------------------------------------------------------------------------
# Supersession candidates between an import and the stored memory
# ---------------------------------------------------------------------------


class PendingConflict(BaseModel):
    """A possible contradiction between a stored item and a newly imported one."""

    category: MemoryCategory
    existing_id: str
    incoming_id: str
    similarity: float = Field(ge=0.0, le=1.0)
    shared_terms: list[str] = Field(default_factory=list)
    reason: str = ""
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def key(self) -> str:
        return f"{self.existing_id}:{self.incoming_id}"


def find_pending_conflicts(
    existing: StructuredMemory,
    incoming: StructuredMemory,
    *,
    merger: PackageMerger | None = None,
) -> list[PendingConflict]:
    """Flag incoming items that may contradict an *active* stored item.

    Uses the merger's conflict detector but only across the boundary (stored
    vs. new), never within one side, and never for items with the same id.
    """
    merger = merger or PackageMerger()
    stored_ids = set(existing.items_by_id())
    found: list[PendingConflict] = []
    for conflict in merger.cross_conflicts(existing.active(), incoming):
        a, b = conflict.a, conflict.b
        if a.id == b.id or b.id in stored_ids:
            continue  # re-seen statement, not a contradiction
        found.append(
            PendingConflict(
                category=conflict.category,
                existing_id=a.id,
                incoming_id=b.id,
                similarity=conflict.similarity,
                shared_terms=conflict.shared_terms,
                reason=conflict.reason,
            )
        )
    return found


def conflict_from_pair(category: MemoryCategory, a: MemoryItem, b: MemoryItem) -> Conflict:
    """Small helper used by tests and the CLI to describe a stored pair."""
    from contextbridge.core.merger import shared_terms, similarity

    return Conflict(
        category=category,
        a=a,
        b=b,
        similarity=similarity(a.content, b.content),
        shared_terms=shared_terms(a.content, b.content)[:8],
    )


PENDING_KEY = "pending_conflicts"


def pending_from_metadata(metadata: dict[str, Any]) -> list[PendingConflict]:
    raw = metadata.get(PENDING_KEY) or []
    result: list[PendingConflict] = []
    for entry in raw:
        try:
            result.append(PendingConflict.model_validate(entry))
        except Exception:  # malformed entry from a hand-edited file: skip it
            continue
    return result


def store_pending(metadata: dict[str, Any], pending: list[PendingConflict]) -> None:
    if pending:
        metadata[PENDING_KEY] = [p.model_dump(mode="json") for p in pending]
    else:
        metadata.pop(PENDING_KEY, None)


def prune_pending(
    memory: StructuredMemory, pending: list[PendingConflict]
) -> list[PendingConflict]:
    """Drop pending conflicts whose items were removed or are no longer both active."""
    by_id = memory.items_by_id()
    keep: list[PendingConflict] = []
    for p in pending:
        a, b = by_id.get(p.existing_id), by_id.get(p.incoming_id)
        if a is None or b is None:
            continue
        if not (a.is_active and b.is_active):
            continue
        keep.append(p)
    return keep


# ---------------------------------------------------------------------------
# Hand-off detection
# ---------------------------------------------------------------------------

#: The provenance footer every ContextBridge prompt carries.
_FOOTER_RE = re.compile(
    r'Context:\s*"(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63})"\s+v(?P<version>\d+)'
)
_PASTE_OPENING = "I'm continuing a conversation from another AI assistant."
_PASTE_CLOSING = "Please confirm you've understood this context, then I'll continue"


class Handoff(BaseModel):
    """A transcript that began with a ContextBridge prompt."""

    package_name: str
    package_version: int
    stripped_chars: int = 0
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def detect_handoff(text: str) -> tuple[str, Handoff | None]:
    """Find a pasted ContextBridge prompt in *text* and cut it out.

    Returns ``(text_without_the_pasted_block, handoff_or_None)``.  Only the
    first pasted block is removed; the footer inside it identifies the source
    package and version.  If a footer is present but the wrapper sentences are
    not (someone pasted only the context block), nothing is stripped but the
    hand-off is still recorded.
    """
    match = _FOOTER_RE.search(text)
    if match is None:
        return text, None
    handoff = Handoff(package_name=match.group("name"), package_version=int(match.group("version")))

    start = text.find(_PASTE_OPENING)
    end = text.find(_PASTE_CLOSING, match.end())
    if start == -1 or end == -1 or start > match.start():
        return text, handoff
    end_line = text.find("\n", end)
    end = len(text) if end_line == -1 else end_line
    stripped = text[:start] + text[end:]
    handoff.stripped_chars = len(text) - len(stripped)
    return stripped, handoff


# ---------------------------------------------------------------------------
# Health report
# ---------------------------------------------------------------------------


def health_report(
    pkg: ContextPackage,
    *,
    egress: list[EgressRecord] | None = None,
    stale_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A one-glance summary of how trustworthy and how exposed a package is."""
    now = now or datetime.now(UTC)
    memory = pkg.memory
    items = memory.all_items
    egress = egress or []

    def age_days(ts: datetime) -> int:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return max(0, (now - ts).days)

    stale = [
        i for i in items if i.is_active and age_days(i.last_seen) >= stale_days and stale_days > 0
    ]
    open_tasks = [i for i in memory.open_tasks if i.is_active]
    corroborated = [i for i in items if i.is_active and i.seen_count >= 2]
    single = [i for i in items if i.is_active and i.seen_count == 1]
    pending = prune_pending(memory, pending_from_metadata(pkg.metadata))

    cloud_ids: set[str] = set()
    local_ids: set[str] = set()
    for record in egress:
        (cloud_ids if record.target_kind == "cloud" else local_ids).update(record.item_ids)
    by_id = memory.items_by_id()
    shared_cloud = [by_id[i] for i in cloud_ids if i in by_id]
    never_shared = [i for i in items if i.id not in cloud_ids and i.id not in local_ids]

    def brief(item: MemoryItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "category": item.category.value,
            "content": item.content,
            "status": item.status.value,
            "sharing": item.sharing.value,
            "seen_count": item.seen_count,
            "days_since_seen": age_days(item.last_seen),
        }

    return {
        "package_name": pkg.name,
        "version": pkg.version,
        "generated_at": now.isoformat(),
        "stale_days": stale_days,
        "counts": {
            "items": len(items),
            **{f"status_{k}": v for k, v in memory.status_counts().items()},
            "open_tasks": len(open_tasks),
            "stale": len(stale),
            "corroborated": len(corroborated),
            "single_sighting": len(single),
            "pending_conflicts": len(pending),
            "shared_with_cloud": len(shared_cloud),
            "never_shared": len(never_shared),
            "egress_events": len(egress),
        },
        "stale_items": [brief(i) for i in sorted(stale, key=lambda i: i.last_seen)][:50],
        "open_tasks": [brief(i) for i in sorted(open_tasks, key=lambda i: i.first_seen)][:50],
        "pending_conflicts": [p.model_dump(mode="json") for p in pending],
        "shared_with_cloud": [brief(i) for i in shared_cloud][:50],
        "handoffs": pkg.metadata.get("handoffs", []),
    }
