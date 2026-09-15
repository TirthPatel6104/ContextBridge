"""
Application service layer.

:class:`ContextBridgeService` is the one place where the pieces are wired
together: extraction → redaction → packaging → storage → retrieval → prompt.
The CLI and the Flask app are thin shells over it, which keeps behaviour
identical across surfaces and lets tests exercise real workflows against a
temporary store without HTTP or a terminal.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from contextbridge.adapters import get_adapter
from contextbridge.config import Settings
from contextbridge.core.lifecycle import (
    Handoff,
    PendingConflict,
    detect_handoff,
    find_pending_conflicts,
    health_report,
    pending_from_metadata,
    prune_pending,
    set_status,
    store_pending,
    supersede,
)
from contextbridge.core.llm_interface import LLMInterface
from contextbridge.core.local_extractor import LocalExtractor
from contextbridge.core.memory import MemoryExtractor, parse_chat_transcript
from contextbridge.core.merger import MergeResult, PackageMerger
from contextbridge.core.packager import ContextPackager
from contextbridge.core.prompt_builder import PromptBuilder, target_display_name
from contextbridge.core.redaction import (
    RedactionPolicy,
    RedactionReport,
    redact_memory,
    scan_text,
)
from contextbridge.core.retriever import MemoryRetriever
from contextbridge.core.sharing import (
    apply_sharing,
    classify_target,
    partition_for_target,
    sharing_counts,
)
from contextbridge.core.tokens import estimate_tokens
from contextbridge.models import (
    ContextPackage,
    EgressRecord,
    ItemStatus,
    MemoryCategory,
    MemoryItem,
    RetrievalOptions,
    RetrievalResult,
    SharingPolicy,
    StructuredMemory,
    WithheldItem,
)
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.storage.portable import dumps_package, import_package
from contextbridge.storage.vector_store import VectorStore
from contextbridge.validation import (
    ValidationError,
    validate_package_name,
    validate_text_size,
)

logger = logging.getLogger(__name__)

LOCAL_ENGINE_NAMES = frozenset({"local", "rules", "offline", "none"})
API_ENGINE_NAMES = frozenset({"openai", "gpt", "claude", "anthropic"})

AdapterFactory = Callable[..., LLMInterface]


def _run(coro):
    """Run a coroutine from synchronous code (CLI / Flask)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a loop (e.g. notebook): run in a fresh thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@dataclass
class ImportOutcome:
    """What happened when text was imported into a package."""

    package: ContextPackage
    extracted: StructuredMemory
    redaction: RedactionReport
    created: bool
    engine: str
    chars: int
    transcript_tokens: int
    added_items: int = 0
    re_seen_items: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    handoff: Handoff | None = None
    conflicts: list[PendingConflict] = field(default_factory=list)


class ContextBridgeService:
    """Facade over the core modules, bound to one storage backend."""

    def __init__(
        self,
        store: StorageBackend,
        *,
        settings: Settings | None = None,
        adapter_factory: AdapterFactory = get_adapter,
        packager: ContextPackager | None = None,
        builder: PromptBuilder | None = None,
        merger: PackageMerger | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or Settings.from_env()
        self._adapter_factory = adapter_factory
        self.packager = packager or ContextPackager()
        self.builder = builder or PromptBuilder()
        self.merger = merger or PackageMerger()

    # -- Engines -------------------------------------------------------------

    def resolve_engine(self, engine: str | None) -> tuple[str, LLMInterface | None]:
        """
        Map a user-facing engine name to ``(label, adapter_or_None)``.

        ``local``/``rules`` → offline rule-based extractor (no adapter).
        ``openai``/``claude`` → vendor adapter.  Anything else is treated as
        an Ollama model name.
        """
        name = (engine or self.settings.default_adapter or "local").strip()
        lower = name.lower()
        if lower in LOCAL_ENGINE_NAMES:
            return "local", None
        if lower in API_ENGINE_NAMES:
            return lower, self._adapter_factory(lower)
        return f"ollama:{name}", self._adapter_factory("local", model=name)

    def extract(self, text: str, *, engine: str = "local", origin: str = "") -> StructuredMemory:
        """Extract structured memory using the requested engine."""
        label, adapter = self.resolve_engine(engine)
        return self._extract_with(text, label, adapter, origin)

    @staticmethod
    def _extract_with(
        text: str, label: str, adapter: LLMInterface | None, origin: str
    ) -> StructuredMemory:
        if adapter is None:
            return LocalExtractor().extract(text, origin=origin)
        extractor = MemoryExtractor()
        return _run(extractor.extract(text, adapter, source_model=label, origin=origin))

    # -- Import --------------------------------------------------------------

    def import_text(
        self,
        text: str,
        package_name: str,
        *,
        engine: str = "local",
        origin: str = "",
        redact: bool | None = None,
        redaction_kinds: list[str] | None = None,
        mode: str = "append",
    ) -> ImportOutcome:
        """
        Extract memory from *text* and store it in *package_name*.

        Parameters
        ----------
        mode
            ``"append"`` merges new items into an existing package (default);
            ``"replace"`` makes the new extraction the package's memory.
        redact
            Run the secrets/PII pass before storing.  Defaults to the
            configured policy (on).
        """
        name = validate_package_name(package_name)
        validate_text_size(text, self.settings.max_text_chars)
        if not text.strip():
            raise ValidationError("No text to import")
        if mode not in {"append", "replace"}:
            raise ValidationError("mode must be 'append' or 'replace'")

        notes: list[str] = []
        text, handoff = detect_handoff(text)
        if handoff is not None:
            if not text.strip():
                raise ValidationError(
                    "The file contains only a pasted ContextBridge prompt and no new "
                    "conversation, so there is nothing to import."
                )
            notes.append(
                f"Hand-off detected: this chat started from package "
                f"'{handoff.package_name}' v{handoff.package_version}."
                + (
                    f" The pasted context ({handoff.stripped_chars:,} chars) was skipped "
                    "so the package does not re-import an echo of itself."
                    if handoff.stripped_chars
                    else ""
                )
            )

        label, adapter = self.resolve_engine(engine)
        memory = self._extract_with(text, label, adapter, origin)

        do_redact = self.settings.redact_by_default if redact is None else redact
        policy = RedactionPolicy(enabled=do_redact, kinds=redaction_kinds)
        memory, report = redact_memory(memory, policy)

        transcript_tokens = parse_chat_transcript(text).total_tokens_estimate
        warnings: list[str] = []
        if memory.total_items == 0:
            warnings.append(
                "No memory items were extracted. Try an LLM engine, or check that the "
                "transcript uses 'User:' / 'Assistant:' turns."
            )

        created = not self.store.exists(name)
        conflicts: list[PendingConflict] = []
        re_seen = 0
        if created:
            pkg = self.packager.create(name, memory, source_model=label)
            added = memory.total_items
        else:
            pkg = self.store.load(name)
            before = set(pkg.memory.items_by_id())
            if mode == "replace":
                pkg = self.packager.update(pkg, memory, source_model=label, note="replaced")
            else:
                conflicts = find_pending_conflicts(pkg.memory, memory, merger=self.merger)
                pkg = self.packager.append(pkg, memory, source_model=label, note="imported")
                re_seen = len(before & set(memory.items_by_id()))
            added = len(set(pkg.memory.items_by_id()) - before)

        if handoff is not None:
            handoffs = list(pkg.metadata.get("handoffs") or [])
            handoffs.append({**handoff.model_dump(mode="json"), "origin": origin})
            pkg.metadata["handoffs"] = handoffs[-50:]
        if conflicts:
            known = {p.key: p for p in pending_from_metadata(pkg.metadata)}
            for c in conflicts:
                known.setdefault(c.key, c)
            store_pending(pkg.metadata, list(known.values()))
            notes.append(
                f"{len(conflicts)} new statement(s) may contradict what the package already "
                "holds. Review them with 'cb conflicts' or in the dashboard."
            )
        self._save(pkg)

        return ImportOutcome(
            package=pkg,
            extracted=memory,
            redaction=report,
            created=created,
            engine=label,
            chars=len(text),
            transcript_tokens=transcript_tokens,
            added_items=added,
            re_seen_items=re_seen,
            warnings=warnings,
            notes=notes,
            handoff=handoff,
            conflicts=conflicts,
        )

    def _save(self, pkg: ContextPackage) -> None:
        """Persist a package after dropping pending conflicts that no longer apply."""
        pending = pending_from_metadata(pkg.metadata)
        if pending:
            store_pending(pkg.metadata, prune_pending(pkg.memory, pending))
        self.store.save(pkg)

    def scan(self, text: str) -> RedactionReport:
        """Report what the redactor *would* mask in *text*, without changing it."""
        validate_text_size(text, self.settings.max_text_chars)
        findings = scan_text(text)
        report = RedactionReport(
            findings=findings, items_total=1, items_redacted=int(bool(findings))
        )
        for f in findings:
            report.counts[f.kind] = report.counts.get(f.kind, 0) + 1
        return report

    # -- Packages ------------------------------------------------------------

    def get_package(self, name: str, *, version: int | None = None) -> ContextPackage:
        return self.store.load(validate_package_name(name), version=version)

    def list_packages(self) -> list[dict[str, Any]]:
        return self.store.summaries()

    def delete_package(self, name: str) -> None:
        name = validate_package_name(name)
        if not self.store.exists(name):
            raise PackageNotFoundError(f"Package '{name}' not found")
        self.store.delete(name)

    def remove_items(self, name: str, item_ids: Iterable[str], *, note: str = "") -> ContextPackage:
        pkg = self.get_package(name)
        ids = self._clean_ids(item_ids)
        pkg = self.packager.remove_items(pkg, ids, note=note or "removed during review")
        self._save(pkg)
        return pkg

    def history(self, name: str) -> list[dict[str, Any]]:
        return self.packager.get_version_summary(self.get_package(name))

    def rollback(self, name: str, version: int) -> ContextPackage:
        pkg = self.get_package(name)
        pkg = self.packager.rollback(pkg, version)
        self._save(pkg)
        return pkg

    @staticmethod
    def _clean_ids(item_ids: Iterable[str]) -> set[str]:
        ids = {i.strip() for i in item_ids if isinstance(i, str) and i.strip()}
        if not ids:
            raise ValidationError("No item ids given")
        return ids

    # -- Sharing boundaries --------------------------------------------------

    def set_sharing(
        self, name: str, item_ids: Iterable[str], policy: SharingPolicy | str
    ) -> tuple[ContextPackage, int]:
        """Apply a sharing policy to items; returns the package and how many items changed."""
        try:
            chosen = SharingPolicy(str(policy).strip().lower())
        except ValueError as exc:
            valid = ", ".join(p.value for p in SharingPolicy)
            raise ValidationError(
                f"Unknown sharing policy {policy!r}. Choose from: {valid}"
            ) from exc
        pkg = self.get_package(name)
        ids = self._clean_ids(item_ids)
        missing = ids - set(pkg.memory.items_by_id())
        if missing:
            raise ValidationError(f"Unknown item id(s): {', '.join(sorted(missing))}")
        memory, changed = apply_sharing(pkg.memory, ids, chosen)
        if changed:
            pkg = self.packager.apply(pkg, memory, note=f"sharing set to {chosen.value}")
            self._save(pkg)
        return pkg, changed

    # -- Lifecycle -----------------------------------------------------------

    def set_status(
        self, name: str, item_ids: Iterable[str], status: ItemStatus | str
    ) -> tuple[ContextPackage, int]:
        """Mark items ``done`` or ``active`` again; use :meth:`supersede` for replacements."""
        try:
            chosen = ItemStatus(str(status).strip().lower())
        except ValueError as exc:
            raise ValidationError(f"Unknown status {status!r}. Choose from: active, done") from exc
        if chosen == ItemStatus.SUPERSEDED:
            raise ValidationError("Use supersede(old_id, new_id) to record a replacement")
        pkg = self.get_package(name)
        ids = self._clean_ids(item_ids)
        missing = ids - set(pkg.memory.items_by_id())
        if missing:
            raise ValidationError(f"Unknown item id(s): {', '.join(sorted(missing))}")
        memory, changed = set_status(pkg.memory, ids, chosen)
        if changed:
            note = "marked done" if chosen == ItemStatus.DONE else "reactivated"
            pkg = self.packager.apply(pkg, memory, note=note)
            self._save(pkg)
        return pkg, changed

    def supersede(self, name: str, old_id: str, new_id: str) -> ContextPackage:
        """Record that *new_id* replaces *old_id*; the old item stays, marked superseded."""
        pkg = self.get_package(name)
        try:
            memory = supersede(pkg.memory, old_id.strip(), new_id.strip())
        except KeyError as exc:
            raise ValidationError(f"Unknown item id: {exc.args[0]}") from exc
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        pkg = self.packager.apply(pkg, memory, note=f"{old_id} superseded by {new_id}")
        self._save(pkg)
        return pkg

    def pending_conflicts(self, name: str) -> list[PendingConflict]:
        pkg = self.get_package(name)
        return prune_pending(pkg.memory, pending_from_metadata(pkg.metadata))

    def resolve_conflict(
        self, name: str, existing_id: str, incoming_id: str, action: str
    ) -> ContextPackage:
        """
        Settle a pending conflict.

        ``keep_new`` supersedes the stored item with the incoming one,
        ``keep_old`` the reverse, and ``dismiss`` records that both may stand.
        """
        action = (action or "").strip().lower()
        if action not in {"keep_new", "keep_old", "dismiss"}:
            raise ValidationError("action must be keep_new, keep_old, or dismiss")
        pkg = self.get_package(name)
        pending = pending_from_metadata(pkg.metadata)
        key = f"{existing_id}:{incoming_id}"
        if not any(p.key == key for p in pending):
            raise ValidationError("No such pending conflict")
        if action == "keep_new":
            memory = supersede(pkg.memory, existing_id, incoming_id)
            pkg = self.packager.apply(
                pkg, memory, note=f"{existing_id} superseded by {incoming_id}"
            )
        elif action == "keep_old":
            memory = supersede(pkg.memory, incoming_id, existing_id)
            pkg = self.packager.apply(
                pkg, memory, note=f"{incoming_id} superseded by {existing_id}"
            )
        store_pending(pkg.metadata, [p for p in pending if p.key != key])
        self._save(pkg)
        return pkg

    # -- Audit & health ------------------------------------------------------

    def audit(self, name: str, *, limit: int = 200) -> dict[str, Any]:
        """The egress ledger for a package plus a per-item "who has seen this" summary."""
        pkg = self.get_package(name)
        records = self.store.egress_records(pkg.name, limit=limit)
        by_id = pkg.memory.items_by_id()
        per_item: dict[str, dict[str, Any]] = {}
        for record in records:
            for item_id in record.item_ids:
                entry = per_item.setdefault(
                    item_id, {"targets": {}, "cloud": False, "last": record.timestamp.isoformat()}
                )
                entry["targets"][record.target_model] = (
                    entry["targets"].get(record.target_model, 0) + 1
                )
                entry["cloud"] = entry["cloud"] or record.target_kind == "cloud"
        items = []
        for item_id, entry in per_item.items():
            item = by_id.get(item_id)
            items.append(
                {
                    "id": item_id,
                    "content": item.content if item else "(item no longer in package)",
                    "category": item.category.value if item else "",
                    "present": item is not None,
                    "targets": entry["targets"],
                    "sent_to_cloud": entry["cloud"],
                    "last_shared_at": entry["last"],
                }
            )
        items.sort(key=lambda e: (not e["sent_to_cloud"], e["id"]))
        cloud_ids = {e["id"] for e in items if e["sent_to_cloud"]}
        return {
            "package_name": pkg.name,
            "version": pkg.version,
            "events": [r.model_dump(mode="json") for r in records],
            "items": items,
            "summary": {
                "events": len(records),
                "items_shared": len(items),
                "items_shared_with_cloud": len(cloud_ids),
                "items_never_shared": sum(1 for i in by_id if i not in per_item),
                "targets": sorted({r.target_model for r in records}),
            },
        }

    def clear_audit(self, name: str) -> int:
        return self.store.clear_egress(validate_package_name(name))

    def health(self, name: str, *, stale_days: int = 30) -> dict[str, Any]:
        pkg = self.get_package(name)
        records = self.store.egress_records(pkg.name, limit=1000)
        return health_report(pkg, egress=records, stale_days=stale_days)

    # -- Retrieval & prompts -------------------------------------------------

    def retrieve(
        self,
        name: str,
        query: str,
        options: RetrievalOptions | None = None,
        *,
        adapter: LLMInterface | None = None,
        transcript_tokens: int | None = None,
    ) -> RetrievalResult:
        """Explainable retrieval over a stored package (lexical unless an adapter is given)."""
        pkg = self.get_package(name)
        return self.retrieve_from_memory(
            pkg.memory, query, options, adapter=adapter, transcript_tokens=transcript_tokens
        )

    def retrieve_from_memory(
        self,
        memory: StructuredMemory,
        query: str,
        options: RetrievalOptions | None = None,
        *,
        adapter: LLMInterface | None = None,
        transcript_tokens: int | None = None,
    ) -> RetrievalResult:
        options = options or RetrievalOptions()
        if adapter is None:
            retriever = MemoryRetriever()
            retriever.index_sync(memory)
            return retriever.retrieve_sync(query, options, transcript_tokens=transcript_tokens)
        retriever = MemoryRetriever(VectorStore())
        _run(retriever.index(memory, adapter))
        return _run(
            retriever.retrieve(query, adapter, options, transcript_tokens=transcript_tokens)
        )

    def build_prompt(
        self,
        name: str,
        target_model: str,
        *,
        query: str | None = None,
        options: RetrievalOptions | None = None,
        extras: Iterable[tuple[str, str]] = (),
        adapter: LLMInterface | None = None,
        surface: str = "cli",
        record: bool = True,
    ) -> dict[str, Any]:
        """
        Build a paste-ready prompt, optionally narrowed by retrieval.

        The package memory is first partitioned by the target's kind: items
        whose sharing policy or status forbids rendering are *withheld* and
        listed in the result.  Unless ``record`` is false, an egress ledger
        entry records which item ids were rendered for which target.

        Returns a dict with the prompt, the context block alone, token
        estimates, the withheld items, and (when a query was given) the
        retrieval result.
        """
        pkg = self.get_package(name)
        allowed, withheld = partition_for_target(pkg.memory, target_model)
        target_kind = classify_target(target_model)

        retrieval: RetrievalResult | None = None
        if query and query.strip():
            retrieval = self.retrieve_from_memory(allowed, query, options, adapter=adapter)
            rendered = retrieval.to_memory()
            note = f"Selected by {retrieval.method} retrieval for: {query.strip()!r}"
        else:
            rendered = allowed
            note = ""
        if withheld:
            note = (note + " | " if note else "") + f"Withheld: {len(withheld)}"
        context_block = self.builder.build(pkg, target_model, memory=rendered, footer_note=note)

        if not context_block.strip():
            hint = " matching the query" if retrieval else ""
            if withheld and not allowed.total_items:
                hint = (
                    f"; all {len(withheld)} item(s) are withheld from this target by their "
                    "sharing policy or status"
                )
            raise ValidationError(f"Nothing to include: the package has no memory items{hint}")

        prompt = self.builder.paste_prompt(context_block, target_model, extras=extras)
        full_block = self.builder.build(pkg, target_model, memory=allowed)
        included_ids = [i.id for i in rendered.all_items]
        tokens_prompt = estimate_tokens(prompt)

        egress: EgressRecord | None = None
        if record:
            egress = self.store.record_egress(
                EgressRecord(
                    package_name=pkg.name,
                    package_version=pkg.version,
                    target_model=target_model,
                    target_kind=target_kind,
                    surface=surface,
                    query=(query or "").strip()[:200],
                    item_ids=included_ids,
                    item_count=len(included_ids),
                    withheld_count=len(withheld),
                    tokens=tokens_prompt,
                )
            )

        return {
            "prompt": prompt,
            "context_block": context_block,
            "target_model": target_model,
            "target_kind": target_kind,
            "target_name": target_display_name(target_model),
            "package_name": pkg.name,
            "version": pkg.version,
            "total_items": pkg.memory.total_items,
            "included_items": len(included_ids),
            "included_ids": included_ids,
            "withheld": withheld,
            "withheld_count": len(withheld),
            "tokens_prompt": tokens_prompt,
            "tokens_full_prompt": estimate_tokens(
                self.builder.paste_prompt(full_block, target_model)
            ),
            "retrieval": retrieval,
            "egress_id": egress.id if egress else None,
        }

    def system_prompt_for_query(
        self,
        name: str,
        question: str,
        target_model: str,
        *,
        adapter: LLMInterface | None = None,
        smart: bool = True,
        top_k: int = 5,
        surface: str = "query",
    ) -> tuple[str, dict[str, Any]]:
        """Context block for ``cb query``: partitioned, optionally narrowed, ledger-recorded."""
        pkg = self.get_package(name)
        allowed, withheld = partition_for_target(pkg.memory, target_model)
        retrieval: RetrievalResult | None = None
        if smart and allowed.total_items > 0:
            retrieval = self.retrieve_from_memory(
                allowed, question, RetrievalOptions(top_k=top_k), adapter=adapter
            )
            rendered = retrieval.to_memory()
        else:
            rendered = allowed
        block = self.builder.build(pkg, target_model, memory=rendered)
        ids = [i.id for i in rendered.all_items]
        self.store.record_egress(
            EgressRecord(
                package_name=pkg.name,
                package_version=pkg.version,
                target_model=target_model,
                target_kind=classify_target(target_model),
                surface=surface,
                query=question.strip()[:200],
                item_ids=ids,
                item_count=len(ids),
                withheld_count=len(withheld),
                tokens=estimate_tokens(block),
            )
        )
        return block, {
            "included_items": len(ids),
            "total_items": pkg.memory.total_items,
            "withheld": withheld,
            "retrieval": retrieval,
        }

    @staticmethod
    def withheld_to_dicts(withheld: list[WithheldItem]) -> list[dict[str, Any]]:
        return [
            {
                "id": w.item.id,
                "category": w.item.category.value,
                "content": w.item.content,
                "reason": w.reason,
                "sharing": w.item.sharing.value,
                "status": w.item.status.value,
            }
            for w in withheld
        ]

    # -- Portability ---------------------------------------------------------

    def export_package_json(self, name: str, *, indent: int | None = 2) -> str:
        return dumps_package(self.get_package(name), indent=indent)

    def import_package_data(
        self,
        data: str | bytes | dict[str, Any],
        *,
        rename: str | None = None,
        overwrite: bool = False,
    ) -> ContextPackage:
        pkg = import_package(data, rename=rename)
        if self.store.exists(pkg.name) and not overwrite:
            raise ValidationError(
                f"Package '{pkg.name}' already exists. Choose a different name or allow overwrite."
            )
        # Ids for legacy items are computed on load; make sure they are persisted.
        for item in pkg.memory.all_items:
            item.id = item.id or ""
        self.store.save(pkg)
        return pkg

    # -- Merging -------------------------------------------------------------

    def merge_packages(
        self,
        names: list[str],
        new_name: str,
        *,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> tuple[ContextPackage, MergeResult]:
        if len(names) < 2:
            raise ValidationError("Merging needs at least two packages")
        target = validate_package_name(new_name)
        packages = [self.get_package(n) for n in names]
        if not dry_run and self.store.exists(target) and not overwrite:
            raise ValidationError(f"Package '{target}' already exists")
        merged, result = self.merger.merge_packages(packages, name=target, packager=self.packager)
        if not dry_run:
            self.store.save(merged)
        return merged, result

    # -- Introspection -------------------------------------------------------

    @staticmethod
    def item_to_dict(item: MemoryItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "category": item.category.value,
            "content": item.content,
            "confidence": item.confidence,
            "source": item.source,
            "origin": item.origin,
            "redactions": [r.model_dump() for r in item.redactions],
            "sharing": item.sharing.value,
            "status": item.status.value,
            "superseded_by": item.superseded_by,
            "seen_count": item.seen_count,
            "first_seen": item.first_seen.isoformat(),
            "last_seen": item.last_seen.isoformat(),
            "tokens": estimate_tokens(item.content),
        }

    @classmethod
    def memory_to_dict(cls, memory: StructuredMemory) -> dict[str, list[dict[str, Any]]]:
        """JSON-friendly memory listing with ids, provenance, policy and lifecycle."""
        return {
            cat.value: [cls.item_to_dict(item) for item in memory.get_category(cat)]
            for cat in MemoryCategory
        }

    def package_to_dict(self, pkg: ContextPackage) -> dict[str, Any]:
        pending = prune_pending(pkg.memory, pending_from_metadata(pkg.metadata))
        by_id = pkg.memory.items_by_id()
        return {
            "name": pkg.name,
            "version": pkg.version,
            "schema_version": pkg.schema_version,
            "source_model": pkg.source_model,
            "total_items": pkg.memory.total_items,
            "active_items": pkg.memory.active().total_items,
            "counts": pkg.memory.counts(),
            "status_counts": pkg.memory.status_counts(),
            "sharing_counts": sharing_counts(pkg.memory),
            "created_at": pkg.created_at.isoformat(),
            "updated_at": pkg.updated_at.isoformat(),
            "metadata": pkg.metadata,
            "memory": self.memory_to_dict(pkg.memory),
            "history": self.packager.get_version_summary(pkg),
            "tokens_full_memory": sum(estimate_tokens(i.content) for i in pkg.memory.all_items),
            "pending_conflicts": [
                {
                    **p.model_dump(mode="json"),
                    "existing": self.item_to_dict(by_id[p.existing_id]),
                    "incoming": self.item_to_dict(by_id[p.incoming_id]),
                }
                for p in pending
            ],
            "handoffs": pkg.metadata.get("handoffs", []),
        }
