"""
Package merging with duplicate and conflict detection.

Combining several conversations into one working memory is where naive
"append everything" breaks down: the same fact shows up phrased three ways,
and two chats may record different decisions about the same thing.  The
merger groups near-duplicates (keeping the most confident phrasing and all of
its origins) and flags *possible* conflicts for a human to resolve.  It never
silently drops a conflicting statement.
"""

from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from contextbridge.core.retriever import tokenize
from contextbridge.models import (
    ContextPackage,
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)

if TYPE_CHECKING:
    from contextbridge.core.packager import ContextPackager

logger = logging.getLogger(__name__)

ORIGIN_SEPARATOR = " | "

_NEGATION_MARKERS = (
    "not ",
    "n't ",
    "never ",
    "no longer",
    "instead",
    "switch",
    "rather than",
    "moved away",
    "dropped",
    "replaced",
    "avoid",
    "without",
    "against",
    "stop ",
)

#: Categories where two similar-but-different statements are worth flagging.
_CONFLICT_CATEGORIES = {
    MemoryCategory.DECISIONS,
    MemoryCategory.PREFERENCES,
    MemoryCategory.FACTS,
    MemoryCategory.IDENTITY,
    MemoryCategory.PROJECTS,
}


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class DuplicateGroup(BaseModel):
    """A kept item plus the near-duplicates folded into it."""

    kept: MemoryItem
    duplicates: list[MemoryItem]
    similarity: float = Field(ge=0.0, le=1.0, description="Lowest similarity in the group")
    origins: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    """Two statements about the same subject that disagree (possibly)."""

    category: MemoryCategory
    a: MemoryItem
    b: MemoryItem
    similarity: float = Field(ge=0.0, le=1.0)
    shared_terms: list[str] = Field(default_factory=list)
    reason: str = ""


class MergeResult(BaseModel):
    memory: StructuredMemory
    sources: list[str] = Field(default_factory=list)
    duplicates: list[DuplicateGroup] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    input_items: int = 0
    kept_items: int = 0

    @property
    def dropped_items(self) -> int:
        return self.input_items - self.kept_items

    def summary(self) -> dict[str, object]:
        return {
            "sources": list(self.sources),
            "input_items": self.input_items,
            "kept_items": self.kept_items,
            "dropped_items": self.dropped_items,
            "duplicate_groups": len(self.duplicates),
            "conflicts": len(self.conflicts),
        }


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def similarity(a: str, b: str) -> float:
    """Blend of token Jaccard and character-level ratio, in ``[0, 1]``."""
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(tokenize(na)), set(tokenize(nb))
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return round(0.5 * jaccard + 0.5 * ratio, 4)


def shared_terms(a: str, b: str) -> list[str]:
    ta, tb = tokenize(a), tokenize(b)
    seen = set(tb)
    return [t for t in dict.fromkeys(ta) if t in seen]


# ---------------------------------------------------------------------------
# Merger
# ---------------------------------------------------------------------------


class PackageMerger:
    """
    Merge several memories / packages into one, reporting what was folded.

    Parameters
    ----------
    duplicate_threshold
        Similarity at or above which two items in the same category are
        treated as the same statement.
    conflict_threshold
        Similarity at or above which two *different* statements in a
        conflict-prone category are flagged for review.
    """

    def __init__(
        self,
        *,
        duplicate_threshold: float = 0.82,
        conflict_threshold: float = 0.45,
    ) -> None:
        if not 0 < conflict_threshold < duplicate_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 < conflict < duplicate <= 1")
        self.duplicate_threshold = duplicate_threshold
        self.conflict_threshold = conflict_threshold

    # -- Public API ----------------------------------------------------------

    def merge_memories(self, inputs: list[tuple[str, StructuredMemory]]) -> MergeResult:
        """Merge ``[(source_label, memory), ...]`` into one reviewed memory."""
        merged = StructuredMemory()
        result = MergeResult(memory=merged, sources=[label for label, _ in inputs])

        for cat in MemoryCategory:
            pool: list[MemoryItem] = []
            for label, memory in inputs:
                for item in memory.get_category(cat):
                    origin = item.origin or label
                    pool.append(item.model_copy(update={"origin": origin}))
            result.input_items += len(pool)
            kept, groups = self._dedupe(pool)
            result.duplicates.extend(groups)
            if cat in _CONFLICT_CATEGORIES:
                result.conflicts.extend(self._find_conflicts(cat, kept))
            merged.set_category(cat, kept)

        result.kept_items = merged.total_items
        logger.info(
            "Merged %d sources: %d → %d items, %d duplicate groups, %d possible conflicts",
            len(inputs),
            result.input_items,
            result.kept_items,
            len(result.duplicates),
            len(result.conflicts),
        )
        return result

    def merge_packages(
        self,
        packages: list[ContextPackage],
        *,
        name: str,
        packager: ContextPackager,
    ) -> tuple[ContextPackage, MergeResult]:
        """Merge packages into a new package called *name*."""
        result = self.merge_memories([(p.name, p.memory) for p in packages])
        source_models = sorted({p.source_model for p in packages if p.source_model})
        merged_pkg = packager.create(
            name,
            result.memory,
            source_model="+".join(source_models) if source_models else "merged",
            metadata={
                "merged_from": [
                    {"name": p.name, "version": p.version, "items": p.memory.total_items}
                    for p in packages
                ],
                "merge_report": result.summary(),
            },
        )
        return merged_pkg, result

    def cross_conflicts(
        self, stored: StructuredMemory, incoming: StructuredMemory
    ) -> list[Conflict]:
        """Possible conflicts between *stored* and *incoming* items only.

        Pairs inside one side are not compared (they were reviewed already or
        arrive together), which makes this suitable for "does this import
        contradict what I already know?" checks.  ``Conflict.a`` is always the
        stored item and ``Conflict.b`` the incoming one.
        """
        conflicts: list[Conflict] = []
        for cat in _CONFLICT_CATEGORIES:
            for a in stored.get_category(cat):
                for b in incoming.get_category(cat):
                    if a.id == b.id:
                        continue
                    conflict = self._conflict_pair(cat, a, b)
                    if conflict is not None:
                        conflicts.append(conflict)
        return conflicts

    # -- Internals -----------------------------------------------------------

    def _conflict_pair(
        self, category: MemoryCategory, a: MemoryItem, b: MemoryItem
    ) -> Conflict | None:
        sim = similarity(a.content, b.content)
        if sim < self.conflict_threshold or sim >= self.duplicate_threshold:
            return None
        shared = shared_terms(a.content, b.content)
        if len(shared) < 2:
            return None
        reason = "Similar statements with different wording"
        la, lb = a.content.lower(), b.content.lower()
        if any(m in la for m in _NEGATION_MARKERS) != any(m in lb for m in _NEGATION_MARKERS):
            reason = "One statement negates or supersedes the other"
        elif a.origin != b.origin:
            reason = "Different conversations describe the same subject differently"
        return Conflict(
            category=category, a=a, b=b, similarity=sim, shared_terms=shared[:8], reason=reason
        )

    def _dedupe(self, pool: list[MemoryItem]) -> tuple[list[MemoryItem], list[DuplicateGroup]]:
        """Greedy clustering: each item joins the first kept item it resembles."""
        kept: list[MemoryItem] = []
        members: list[list[MemoryItem]] = []
        min_sims: list[float] = []
        # Origins are reported in input order (first source first), independent
        # of which phrasing ends up being kept.
        first_seen: dict[str, int] = {}
        for idx, item in enumerate(pool):
            first_seen.setdefault(item.origin, idx)

        # Higher confidence / longer content first so the best phrasing is kept.
        ordered = sorted(pool, key=lambda i: (-i.confidence, -len(i.content), i.origin, i.id))
        for item in ordered:
            placed = False
            for idx, anchor in enumerate(kept):
                sim = similarity(anchor.content, item.content)
                if sim >= self.duplicate_threshold:
                    members[idx].append(item)
                    min_sims[idx] = min(min_sims[idx], sim)
                    placed = True
                    break
            if not placed:
                kept.append(item)
                members.append([])
                min_sims.append(1.0)

        groups: list[DuplicateGroup] = []
        final: list[MemoryItem] = []
        for anchor, dups, sim in zip(kept, members, min_sims):
            origins = _merge_origins([anchor.origin, *(d.origin for d in dups)])
            origins.sort(key=lambda o: first_seen.get(o, len(pool)))
            merged_item = anchor.model_copy(update={"origin": ORIGIN_SEPARATOR.join(origins)})
            final.append(merged_item)
            if dups:
                groups.append(
                    DuplicateGroup(
                        kept=merged_item, duplicates=dups, similarity=sim, origins=origins
                    )
                )
        return final, groups

    def _find_conflicts(self, category: MemoryCategory, items: list[MemoryItem]) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                conflict = self._conflict_pair(category, items[i], items[j])
                if conflict is not None:
                    conflicts.append(conflict)
        return conflicts


def _merge_origins(origins: list[str]) -> list[str]:
    seen: list[str] = []
    for origin in origins:
        for part in (origin or "").split(ORIGIN_SEPARATOR):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return seen
