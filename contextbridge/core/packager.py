"""
Context Packager — creates, versions, and diffs portable context packages.

A ContextPackage is the core data artifact that travels between models.
It contains structured memory plus a full version history with diffs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

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
        pkg = packager.update(pkg, new_memory, source_model="claude-3-haiku")
        diff = packager.diff(old_memory, new_memory)
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
        """
        Create a new context package (v1).

        Parameters
        ----------
        name : str
            A human-readable name for this context.
        memory : StructuredMemory
            The extracted structured memory to package.
        source_model : str, optional
            The model that produced the original conversation.
        metadata : dict, optional
            Arbitrary metadata (e.g. project tags, description).
        """
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
                    diff=ContextDiff(),  # first version has no diff
                    source_model=source_model,
                )
            ],
            metadata=metadata or {},
        )
        logger.info(
            "Created context package '%s' v1 (%d items)",
            name,
            memory.total_items,
        )
        return package

    def update(
        self,
        package: ContextPackage,
        new_memory: StructuredMemory,
        *,
        source_model: str = "",
    ) -> ContextPackage:
        """
        Update a context package with new memory, bumping the version.

        Computes the diff between old and new memory and records it in
        the version history.
        """
        diff = self.diff(package.memory, new_memory)
        package.memory = new_memory
        package.bump_version(diff, source_model)
        logger.info(
            "Updated package '%s' → v%d (+%d/-%d items)",
            package.name,
            package.version,
            len(diff.added),
            len(diff.removed),
        )
        return package

    def diff(
        self,
        old_memory: StructuredMemory,
        new_memory: StructuredMemory,
    ) -> ContextDiff:
        """
        Compute the diff between two StructuredMemory instances.

        Returns
        -------
        ContextDiff
            Items that were added, removed, or modified.
        """
        old_contents = {item.content for item in old_memory.all_items}
        new_contents = {item.content for item in new_memory.all_items}

        added = [item for item in new_memory.all_items if item.content not in old_contents]
        removed = [item for item in old_memory.all_items if item.content not in new_contents]

        return ContextDiff(added=added, removed=removed)

    def rollback(
        self,
        package: ContextPackage,
        target_version: int,
    ) -> ContextPackage:
        """
        Roll back a context package to a previous version.

        This replays the version history backwards, unapplying diffs.

        Parameters
        ----------
        package : ContextPackage
            The current context package.
        target_version : int
            The version to roll back to (must be >= 1).

        Returns
        -------
        ContextPackage
            The rolled-back package (mutated in-place and returned).

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
            "Rolling back '%s' from v%d → v%d",
            package.name,
            package.version,
            target_version,
        )

        # Replay diffs backwards
        while package.version > target_version:
            # The last history entry corresponds to the bump that created
            # the current version.  Its diff tells us what changed.
            if not package.history:
                break

            last = package.history[-1]
            # Undo: remove added items, re-add removed items
            added_contents = {item.content for item in last.diff.added}
            removed_items = list(last.diff.removed)

            # Remove newly-added items from memory
            for cat in MemoryCategory:
                items = package.memory.get_category(cat)
                filtered = [i for i in items if i.content not in added_contents]
                setattr(package.memory, cat.value, filtered)

            # Re-add removed items
            for item in removed_items:
                current = package.memory.get_category(item.category)
                current.append(item)

            package.history.pop()
            package.version -= 1

        package.updated_at = datetime.now(UTC)
        return package

    @staticmethod
    def get_version_summary(package: ContextPackage) -> list[dict]:
        """
        Get a human-readable summary of all versions.

        Returns a list of dicts with version info for display.
        """
        summaries = []
        for entry in package.history:
            summaries.append(
                {
                    "version": entry.version,
                    "timestamp": entry.timestamp.isoformat(),
                    "source_model": entry.source_model,
                    "added": len(entry.diff.added),
                    "removed": len(entry.diff.removed),
                }
            )
        return summaries
