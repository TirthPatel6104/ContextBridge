"""Shared test fixtures for ContextBridge."""

from __future__ import annotations

import json

import pytest

from contextbridge.core.llm_interface import LLMInterface
from contextbridge.models import (
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)

# ---------------------------------------------------------------------------
# Mock LLM Adapter
# ---------------------------------------------------------------------------


class MockAdapter(LLMInterface):
    """Mock adapter for testing — returns deterministic responses."""

    def __init__(self) -> None:
        self._send_response = ""
        self._embed_dimension = 256

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model_id(self) -> str:
        return "mock-v1"

    def set_response(self, response: str) -> None:
        self._send_response = response

    async def send(self, prompt, *, system_prompt="", temperature=0.3, max_tokens=4096) -> str:
        return self._send_response

    async def summarize(self, text, *, max_length=500) -> str:
        return f"Summary: {text[:100]}"

    async def embed(self, text: str) -> list[float]:
        import hashlib

        h = hashlib.sha256(text.encode()).digest()
        vec: list[float] = []
        seed = h
        while len(vec) < self._embed_dimension:
            seed = hashlib.sha256(seed).digest()
            # Map bytes to [-1, 1] range to avoid overflow
            vec.extend((b / 127.5 - 1.0) for b in seed)
        vec = vec[: self._embed_dimension]
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm > 0 else vec


@pytest.fixture
def mock_adapter():
    return MockAdapter()


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_CHAT = (
    "User: Hi, I'm Tirth. I'm a CS student working on an AI portfolio project.\n\n"
    "Assistant: Nice to meet you, Tirth! What kind of project are you building?\n\n"
    "User: I'm building ContextBridge — a cross-model memory system for LLMs. "
    "I want to use Python with FastAPI, and I prefer clean architecture.\n\n"
    "Assistant: That sounds great! Let's start with the core memory extraction.\n\n"
    "User: I decided to use Pydantic for data models and FAISS for vector search.\n\n"
    "Assistant: Good choices. Next we should implement the CLI.\n\n"
    "User: Yes, and I still need to write the evaluation experiments."
)

SAMPLE_EXTRACTION_RESPONSE = json.dumps(
    {
        "identity": [
            {
                "content": "User is Tirth, a CS student",
                "confidence": 0.95,
                "source": "I'm Tirth. I'm a CS student",
            },
        ],
        "projects": [
            {
                "content": "Building ContextBridge — a cross-model memory system for LLMs",
                "confidence": 0.9,
                "source": "building ContextBridge",
            },
        ],
        "facts": [
            {
                "content": "Tech stack: Python with FastAPI",
                "confidence": 0.85,
                "source": "use Python with FastAPI",
            },
        ],
        "decisions": [
            {
                "content": "Using Pydantic for data models",
                "confidence": 0.9,
                "source": "decided to use Pydantic",
            },
            {
                "content": "Using FAISS for vector search",
                "confidence": 0.9,
                "source": "FAISS for vector search",
            },
        ],
        "open_tasks": [
            {
                "content": "Write evaluation experiments",
                "confidence": 0.8,
                "source": "still need to write the evaluation",
            },
            {
                "content": "Implement the CLI",
                "confidence": 0.7,
                "source": "Next we should implement the CLI",
            },
        ],
        "preferences": [
            {
                "content": "Prefers clean architecture",
                "confidence": 0.85,
                "source": "I prefer clean architecture",
            },
        ],
    }
)


@pytest.fixture
def sample_memory() -> StructuredMemory:
    """Pre-built StructuredMemory for testing."""
    return StructuredMemory(
        identity=[
            MemoryItem(category=MemoryCategory.IDENTITY, content="User is Tirth, a CS student")
        ],
        projects=[
            MemoryItem(
                category=MemoryCategory.PROJECTS,
                content="Building ContextBridge — a cross-model memory system",
            )
        ],
        facts=[
            MemoryItem(category=MemoryCategory.FACTS, content="Tech stack: Python with FastAPI")
        ],
        decisions=[
            MemoryItem(category=MemoryCategory.DECISIONS, content="Using Pydantic for data models"),
            MemoryItem(category=MemoryCategory.DECISIONS, content="Using FAISS for vector search"),
        ],
        open_tasks=[
            MemoryItem(category=MemoryCategory.OPEN_TASKS, content="Write evaluation experiments")
        ],
        preferences=[
            MemoryItem(category=MemoryCategory.PREFERENCES, content="Prefers clean architecture")
        ],
    )
