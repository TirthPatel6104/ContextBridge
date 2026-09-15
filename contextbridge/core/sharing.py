"""
Sharing boundaries — decide which memory items a target model may see.

Vendor memories send everything they hold to the vendor by definition.
ContextBridge lets each item carry a :class:`~contextbridge.models.SharingPolicy`
so that, for example, a salary figure or an internal code name can stay in your
local memory (and be rendered for an Ollama model) without ever being pasted
into ChatGPT or Claude.  Every prompt build partitions the memory into what is
rendered and what is *withheld*, and reports both — the user always sees what
was held back and why.
"""

from __future__ import annotations

from contextbridge.models import (
    ItemStatus,
    MemoryItem,
    SharingPolicy,
    StructuredMemory,
    WithheldItem,
)

#: Target names that run on this machine.  Anything else is treated as a
#: cloud target — the conservative default for an unknown name.
LOCAL_TARGET_MARKERS: tuple[str, ...] = ("local", "ollama", "llama", "mistral", "gemma", "phi")

CLOUD = "cloud"
LOCAL = "local"


def classify_target(target_model: str) -> str:
    """Return ``"local"`` or ``"cloud"`` for a target model name."""
    lower = (target_model or "").strip().lower()
    if not lower:
        return CLOUD
    if any(marker in lower for marker in LOCAL_TARGET_MARKERS):
        return LOCAL
    return CLOUD


def withheld_reason(item: MemoryItem, target_kind: str) -> str | None:
    """Why *item* must not be rendered for a target of *target_kind* (None = allowed)."""
    if item.status == ItemStatus.SUPERSEDED:
        return "superseded" + (f" by {item.superseded_by}" if item.superseded_by else "")
    if item.status == ItemStatus.DONE:
        return "marked done"
    if item.sharing == SharingPolicy.NEVER:
        return "sharing policy: never share"
    if item.sharing == SharingPolicy.LOCAL_ONLY and target_kind != LOCAL:
        return "sharing policy: local models only"
    return None


def partition_for_target(
    memory: StructuredMemory, target_model: str
) -> tuple[StructuredMemory, list[WithheldItem]]:
    """Split *memory* into what may be rendered for *target_model* and what may not."""
    kind = classify_target(target_model)
    withheld: list[WithheldItem] = []

    def allowed(item: MemoryItem) -> bool:
        reason = withheld_reason(item, kind)
        if reason is None:
            return True
        withheld.append(WithheldItem(item=item, reason=reason))
        return False

    return memory.select(allowed), withheld


def apply_sharing(
    memory: StructuredMemory, item_ids: set[str] | list[str], policy: SharingPolicy
) -> tuple[StructuredMemory, int]:
    """Return a copy of *memory* with *policy* applied to the given ids, plus the change count."""
    targets = set(item_ids)
    changed = 0
    updated = StructuredMemory()
    for item in memory.all_items:
        if item.id in targets and item.sharing != policy:
            item = item.model_copy(update={"sharing": policy})
            changed += 1
        updated.get_category(item.category).append(item)
    return updated, changed


def sharing_counts(memory: StructuredMemory) -> dict[str, int]:
    counts = {policy.value: 0 for policy in SharingPolicy}
    for item in memory.all_items:
        counts[item.sharing.value] += 1
    return counts
