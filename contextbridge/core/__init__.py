"""Core package — LLM interface, memory extraction, packaging, retrieval, privacy."""

from contextbridge.core.llm_interface import LLMInterface
from contextbridge.core.local_extractor import LocalExtractor
from contextbridge.core.memory import MemoryExtractor
from contextbridge.core.merger import PackageMerger
from contextbridge.core.packager import ContextPackager
from contextbridge.core.prompt_builder import PromptBuilder
from contextbridge.core.redaction import RedactionPolicy, redact_memory
from contextbridge.core.retriever import MemoryRetriever

__all__ = [
    "LLMInterface",
    "LocalExtractor",
    "MemoryExtractor",
    "PackageMerger",
    "ContextPackager",
    "PromptBuilder",
    "MemoryRetriever",
    "RedactionPolicy",
    "redact_memory",
]
