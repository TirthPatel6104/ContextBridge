"""Core package — LLM interface, memory extraction, packaging, retrieval."""

from contextbridge.core.llm_interface import LLMInterface
from contextbridge.core.memory import MemoryExtractor
from contextbridge.core.packager import ContextPackager
from contextbridge.core.prompt_builder import PromptBuilder
from contextbridge.core.retriever import MemoryRetriever

__all__ = [
    "LLMInterface",
    "MemoryExtractor",
    "ContextPackager",
    "PromptBuilder",
    "MemoryRetriever",
]
