"""Abstract LLM interface — model-agnostic adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMInterface(ABC):
    """
    Vendor-independent interface for interacting with large language models.

    Every adapter (OpenAI, Claude, local) implements this contract so that
    upstream modules (memory extraction, summarisation, embedding) never
    depend on a specific provider.
    """

    #: Whether :meth:`embed` returns *semantic* vectors.  Adapters that fall
    #: back to hash-based pseudo-embeddings must set this to ``False`` so the
    #: retriever does not blend noise into its scores.
    semantic_embeddings: bool = True

    # Informational ---------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable adapter name (e.g. 'openai', 'claude')."""
        ...

    @property
    @abstractmethod
    def model_id(self) -> str:
        """The concrete model identifier used for generation."""
        ...

    # Core operations -------------------------------------------------------

    @abstractmethod
    async def send(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send a prompt to the LLM and return the text response.

        Parameters
        ----------
        prompt : str
            The user / main prompt.
        system_prompt : str, optional
            System-level instructions prepended to the conversation.
        temperature : float, optional
            Sampling temperature (0 = deterministic).
        max_tokens : int, optional
            Maximum tokens in the response.

        Returns
        -------
        str
            The model's text response.
        """
        ...

    @abstractmethod
    async def summarize(self, text: str, *, max_length: int = 500) -> str:
        """
        Produce a concise summary of the given text.

        Parameters
        ----------
        text : str
            The text to summarise.
        max_length : int, optional
            Approximate maximum length of the summary in words.

        Returns
        -------
        str
            A concise summary.
        """
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """
        Generate a vector embedding for the given text.

        Parameters
        ----------
        text : str
            The text to embed.

        Returns
        -------
        list[float]
            The embedding vector.
        """
        ...

    # Convenience -----------------------------------------------------------

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed multiple texts.  Default implementation calls *embed* in a
        loop; adapters may override with a batched API call.
        """
        return [await self.embed(t) for t in texts]
