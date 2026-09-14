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
from contextbridge.core.tokens import estimate_tokens
from contextbridge.models import (
    ContextPackage,
    MemoryCategory,
    RetrievalOptions,
    RetrievalResult,
    StructuredMemory,
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
    warnings: list[str] = field(default_factory=list)


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
        if created:
            pkg = self.packager.create(name, memory, source_model=label)
            added = memory.total_items
        else:
            pkg = self.store.load(name)
            before = set(pkg.memory.items_by_id())
            if mode == "replace":
                pkg = self.packager.update(pkg, memory, source_model=label, note="replaced")
            else:
                pkg = self.packager.append(pkg, memory, source_model=label, note="imported")
            added = len(set(pkg.memory.items_by_id()) - before)
        self.store.save(pkg)

        return ImportOutcome(
            package=pkg,
            extracted=memory,
            redaction=report,
            created=created,
            engine=label,
            chars=len(text),
            transcript_tokens=transcript_tokens,
            added_items=added,
            warnings=warnings,
        )

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
        ids = {i for i in item_ids if isinstance(i, str) and i}
        if not ids:
            raise ValidationError("No item ids given")
        pkg = self.packager.remove_items(pkg, ids, note=note or "removed during review")
        self.store.save(pkg)
        return pkg

    def history(self, name: str) -> list[dict[str, Any]]:
        return self.packager.get_version_summary(self.get_package(name))

    def rollback(self, name: str, version: int) -> ContextPackage:
        pkg = self.get_package(name)
        pkg = self.packager.rollback(pkg, version)
        self.store.save(pkg)
        return pkg

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
    ) -> dict[str, Any]:
        """
        Build a paste-ready prompt, optionally narrowed by retrieval.

        Returns a dict with the prompt, the context block alone, token
        estimates, and (when a query was given) the retrieval result.
        """
        pkg = self.get_package(name)
        retrieval: RetrievalResult | None = None
        if query and query.strip():
            retrieval = self.retrieve_from_memory(pkg.memory, query, options, adapter=adapter)
            context_block = self.builder.build_from_retrieval(pkg, retrieval, target_model)
        else:
            context_block = self.builder.build(pkg, target_model)

        if not context_block.strip():
            raise ValidationError(
                "Nothing to include: the package has no memory items"
                + (" matching the query" if retrieval else "")
            )

        prompt = self.builder.paste_prompt(context_block, target_model, extras=extras)
        full_block = self.builder.build(pkg, target_model)
        return {
            "prompt": prompt,
            "context_block": context_block,
            "target_model": target_model,
            "target_name": target_display_name(target_model),
            "package_name": pkg.name,
            "version": pkg.version,
            "total_items": pkg.memory.total_items,
            "included_items": retrieval.selected.__len__() if retrieval else pkg.memory.total_items,
            "tokens_prompt": estimate_tokens(prompt),
            "tokens_full_prompt": estimate_tokens(
                self.builder.paste_prompt(full_block, target_model)
            ),
            "retrieval": retrieval,
        }

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
    def memory_to_dict(memory: StructuredMemory) -> dict[str, list[dict[str, Any]]]:
        """JSON-friendly memory listing with ids, provenance and redaction flags."""
        return {
            cat.value: [
                {
                    "id": item.id,
                    "category": item.category.value,
                    "content": item.content,
                    "confidence": item.confidence,
                    "source": item.source,
                    "origin": item.origin,
                    "redactions": [r.model_dump() for r in item.redactions],
                    "tokens": estimate_tokens(item.content),
                }
                for item in memory.get_category(cat)
            ]
            for cat in MemoryCategory
        }

    def package_to_dict(self, pkg: ContextPackage) -> dict[str, Any]:
        return {
            "name": pkg.name,
            "version": pkg.version,
            "schema_version": pkg.schema_version,
            "source_model": pkg.source_model,
            "total_items": pkg.memory.total_items,
            "counts": pkg.memory.counts(),
            "created_at": pkg.created_at.isoformat(),
            "updated_at": pkg.updated_at.isoformat(),
            "metadata": pkg.metadata,
            "memory": self.memory_to_dict(pkg.memory),
            "history": self.packager.get_version_summary(pkg),
            "tokens_full_memory": sum(estimate_tokens(i.content) for i in pkg.memory.all_items),
        }
