"""
Context Packager — creates, versions, and diffs portable context packages.

A ContextPackage is the core data artifact that travels between models.
It contains structured memory plus a full version history with diffs.
Items are compared by their stable id (category + normalised content), so
re-ordering or whitespace changes never register as edits.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from contextbridge.models import (
    ContextDiff,
    ContextPackage,
    ContextVersion,
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)

logger = logging.getLogger(__name__)


class ContextPackager:
    """
    Creates, updates, and versions ContextPackage objects.

    Usage::

        packager = ContextPackager()
        pkg = packager.create("my_project", memory, source_model="gpt-4o")
        pkg = packager.update(pkg, new_memory, source_model="claude")
        pkg = packager.remove_items(pkg, {"3f2a...": True}, note="review")
        pkg = packager.rollback(pkg, target_version=1)
    """

    def create(
        self,
        name: str,
        memory: StructuredMemory,
        *,
        source_model: str = "",
        metadata: dict | None = None,
    ) -> ContextPackage:
        """Create a new context package (v1)."""
        now = datetime.now(UTC)
        package = ContextPackage(
            name=name,
            version=1,
            created_at=now,
            updated_at=now,
            source_model=source_model,
            memory=memory,
            history=[
                ContextVersion(
                    version=1,
                    timestamp=now,
                    diff=ContextDiff(added=list(memory.all_items)),
                    source_model=source_model,
                    note="created",
                )
            ],
            metadata=metadata or {},
        )
        logger.info("Created context package '%s' v1 (%d items)", name, memory.total_items)
        return package

    def update(
        self,
        package: ContextPackage,
        new_memory: StructuredMemory,
        *,
        source_model: str = "",
        note: str = "",
    ) -> ContextPackage:
        """Replace the package memory, bumping the version and recording the diff."""
        diff = self.diff(package.memory, new_memory)
        package.memory = new_memory
        package.bump_version(diff, source_model, note=note)
        logger.info(
            "Updated package '%s' → v%d (+%d/-%d items)",
            package.name,
            package.version,
            len(diff.added),
            len(diff.removed),
        )
        return package

    def append(
        self,
        package: ContextPackage,
        extra_memory: StructuredMemory,
        *,
        source_model: str = "",
        note: str = "",
    ) -> ContextPackage:
        """Add new items to the existing memory as a new version.

        Items that already exist (same id) are not duplicated; instead their
        sighting counters and origins are updated (see
        :func:`contextbridge.core.lifecycle.absorb`).
        """
        from contextbridge.core.lifecycle import absorb

        merged, _added, _seen = absorb(package.memory, extra_memory)
        return self.update(package, merged, source_model=source_model, note=note)

    def apply(
        self,
        package: ContextPackage,
        new_memory: StructuredMemory,
        *,
        note: str,
    ) -> ContextPackage:
        """Record a status / sharing change as a new version (no-op if nothing changed)."""
        if self.diff(package.memory, new_memory).is_empty:
            return package
        return self.update(package, new_memory, note=note)

    def remove_items(
        self,
        package: ContextPackage,
        item_ids: set[str] | list[str],
        *,
        note: str = "removed during review",
    ) -> ContextPackage:
        """Drop the given item ids as a new version (no-op if nothing matches)."""
        drop = set(item_ids)
        existing = package.memory.items_by_id()
        actually = [i for i in drop if i in existing]
        if not actually:
            return package
        return self.update(package, package.memory.without_ids(drop), note=note)

    def diff(self, old_memory: StructuredMemory, new_memory: StructuredMemory) -> ContextDiff:
        """Compute the diff between two StructuredMemory instances (by item id).

        Besides added / removed items, lifecycle and sharing changes on items
        that kept their id are recorded as ``modified`` entries so that a
        rollback can undo them.
        """
        old_by_id = old_memory.items_by_id()
        new_by_id = new_memory.items_by_id()
        added = [item for item in new_memory.all_items if item.id not in old_by_id]
        removed = [item for item in old_memory.all_items if item.id not in new_by_id]
        modified: list[dict[str, Any]] = []
        for item_id, new_item in new_by_id.items():
            old_item = old_by_id.get(item_id)
            if old_item is None:
                continue
            before, after = old_item.tracked_fields(), new_item.tracked_fields()
            if before != after:
                modified.append(
                    {
                        "id": item_id,
                        "category": new_item.category.value,
                        "before": before,
                        "after": after,
                    }
                )
        return ContextDiff(added=added, removed=removed, modified=modified)

    def rollback(self, package: ContextPackage, target_version: int) -> ContextPackage:
        """
        Roll back a context package to a previous version by un-applying diffs.

        Raises
        ------
        ValueError
            If target_version is out of range.
        """
        if target_version < 1:
            raise ValueError("Cannot roll back to version < 1")
        if target_version >= package.version:
            raise ValueError(
                f"Target version {target_version} must be less than "
                f"current version {package.version}"
            )

        logger.info(
            "Rolling back '%s' from v%d → v%d", package.name, package.version, target_version
        )

        while package.version > target_version and package.history:
            last = package.history[-1]
            added_ids = {item.id for item in last.diff.added}
            for cat in MemoryCategory:
                items = package.memory.get_category(cat)
                package.memory.set_category(cat, [i for i in items if i.id not in added_ids])
            for item in last.diff.removed:
                package.memory.get_category(item.category).append(item)
            if last.diff.modified:
                restore = {m["id"]: m.get("before", {}) for m in last.diff.modified}
                for cat in MemoryCategory:
                    items = package.memory.get_category(cat)
                    package.memory.set_category(
                        cat,
                        [
                            # Re-validate so enum fields come back as enums, not strings.
                            MemoryItem.model_validate({**i.model_dump(), **restore[i.id]})
                            if i.id in restore
                            else i
                            for i in items
                        ],
                    )
            package.history.pop()
            package.version -= 1

        package.updated_at = datetime.now(UTC)
        return package

    @staticmethod
    def get_version_summary(package: ContextPackage) -> list[dict[str, Any]]:
        """Human-readable summary of all versions, oldest first."""
        return [
            {
                "version": entry.version,
                "timestamp": entry.timestamp.isoformat(),
                "source_model": entry.source_model,
                "note": entry.note,
                "added": len(entry.diff.added),
                "removed": len(entry.diff.removed),
                "modified": len(entry.diff.modified),
            }
            for entry in package.history
        ]
