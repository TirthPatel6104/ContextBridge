"""Claude adapter — Anthropic's Claude models via the Anthropic API."""

from __future__ import annotations

import os

from anthropic import AsyncAnthropic

from contextbridge.core.llm_interface import LLMInterface


class ClaudeAdapter(LLMInterface):
    """
    LLM adapter for Anthropic's Claude models.

    Uses ``claude-3-haiku-20240307`` for generation/summarisation.

    Embeddings: Claude does not natively support embeddings, so this
    adapter returns a deterministic hash-based pseudo-embedding.  It is
    flagged as non-semantic, so the retriever relies on lexical scoring.
    """

    semantic_embeddings = False

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-haiku-20240307",
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
        self._model = model

    # -- Properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "claude"

    @property
    def model_id(self) -> str:
        return self._model

    # -- Core operations ----------------------------------------------------

    async def send(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await self._client.messages.create(**kwargs)
        # Claude returns a list of content blocks
        return "".join(block.text for block in response.content if hasattr(block, "text"))

    async def summarize(self, text: str, *, max_length: int = 500) -> str:
        system = (
            f"Summarise the following text in at most {max_length} words. "
            "Be concise and preserve key information."
        )
        return await self.send(text, system_prompt=system, temperature=0.2)

    async def embed(self, text: str) -> list[float]:
        """
        Lightweight local embedding fallback.

        Claude doesn't provide an embedding API, so we use a deterministic
        hash-based embedding for structural similarity. This is sufficient
        for local retrieval; for production, integrate a dedicated embedding
        service (e.g. OpenAI embeddings, or sentence-transformers).
        """
        return self._hash_embed(text)

    @staticmethod
    def _hash_embed(text: str, dim: int = 256) -> list[float]:
        """
        Deterministic pseudo-embedding via character-level hashing.

        NOT a semantic embedding — purely structural. Good enough for
        basic similarity when no embedding API is available.
        """
        import hashlib
        import struct

        # Create a deterministic seed from the text
        text_hash = hashlib.sha256(text.encode("utf-8")).digest()
        # Expand to desired dimension via repeated hashing
        vector: list[float] = []
        seed = text_hash
        while len(vector) < dim:
            seed = hashlib.sha256(seed).digest()
            # Unpack 8 floats from 32 bytes
            floats = struct.unpack("8f", seed)
            vector.extend(floats)

        # Truncate and normalise
        vector = vector[:dim]
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector
