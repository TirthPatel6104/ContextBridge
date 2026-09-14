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
        """Add new items to the existing memory (exact-duplicate safe) as a new version."""
        return self.update(
            package, package.memory.merge(extra_memory), source_model=source_model, note=note
        )

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
        """Compute the diff between two StructuredMemory instances (by item id)."""
        old_ids = {item.id for item in old_memory.all_items}
        new_ids = {item.id for item in new_memory.all_items}
        added = [item for item in new_memory.all_items if item.id not in old_ids]
        removed = [item for item in old_memory.all_items if item.id not in new_ids]
        return ContextDiff(added=added, removed=removed)

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
            }
            for entry in package.history
        ]
