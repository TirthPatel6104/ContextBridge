"""OpenAI adapter — GPT models via the OpenAI API."""

from __future__ import annotations

import os

from openai import AsyncOpenAI

from contextbridge.core.llm_interface import LLMInterface


class OpenAIAdapter(LLMInterface):
    """
    LLM adapter for OpenAI's GPT models.

    Uses ``gpt-4o-mini`` for generation/summarisation and
    ``text-embedding-3-small`` for embeddings.

    Reads ``OPENAI_API_KEY`` from the environment if no key is passed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self._model = model
        self._embedding_model = embedding_model

    # -- Properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "openai"

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
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    async def summarize(self, text: str, *, max_length: int = 500) -> str:
        system = (
            f"Summarise the following text in at most {max_length} words. "
            "Be concise and preserve key information."
        )
        return await self.send(text, system_prompt=system, temperature=0.2)

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self._embedding_model,
            input=text,
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batched embedding — OpenAI supports multi-input in one call."""
        response = await self._client.embeddings.create(
            model=self._embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]
