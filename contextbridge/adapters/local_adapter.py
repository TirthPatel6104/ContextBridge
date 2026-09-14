"""Local adapter — integration for local LLMs via Ollama."""

from __future__ import annotations

import httpx

from contextbridge.core.llm_interface import LLMInterface


class LocalAdapter(LLMInterface):
    """
    Adapter for local LLM inference via Ollama.

    Expects Ollama to be running on localhost:11434 (default).
    Supports any model pulled via `ollama pull <model>`.

    Embeddings are only semantic when the configured model supports the
    ``/api/embeddings`` endpoint; failures return an empty list, which the
    retriever treats as "no embeddings" and falls back to lexical scoring.
    """

    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434") -> None:
        self._model = model
        self.base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model_id(self) -> str:
        return self._model

    async def send(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """Send a prompt to the local Ollama instance."""
        url = f"{self.base_url}/api/generate"

        # Ollama accepts the system prompt as a separate field.
        payload = {
            "model": self._model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "temperature": temperature,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                return data.get("response", "")
        except httpx.ConnectError:
            return "Error: Could not connect to Ollama. Is it running on port 11434?"
        except Exception as e:
            return f"Error calling Ollama: {str(e)}"

    async def summarize(self, text: str, *, max_length: int = 500) -> str:
        """Summarize text using the local model."""
        prompt = (
            f"Please summarize the following text in about {max_length} characters, "
            f"preserving key details:\n\n{text}"
        )
        return await self.send(prompt, temperature=0.2)

    async def embed(self, text: str) -> list[float]:
        """Generate embeddings using Ollama (requires an embedding model like nomic-embed-text)."""
        url = f"{self.base_url}/api/embeddings"

        # Prefer an embedding model if available, otherwise try the main model
        # Note: generic LLMs might not support /api/embeddings endpoint well in old Ollama versions
        # Ideally user should select an embedding model. For now we use the main model
        # or fall back to a hardcoded logic if we could detect available models.
        # Let's try the configured model.

        payload = {
            "model": self._model,
            "prompt": text,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 404:
                    # Model doesn't support embeddings or endpoint not found
                    return []
                response.raise_for_status()
                data = response.json()
                return data.get("embedding", [])
        except Exception:
            return []

    @classmethod
    async def list_models(cls, base_url: str = "http://localhost:11434") -> list[str]:
        """Fetch list of available models from Ollama."""
        url = f"{base_url}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    return [m["name"] for m in data.get("models", [])]
        except Exception:
            pass
        return []
