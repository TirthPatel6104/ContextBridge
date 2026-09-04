"""Adapter package — factory for LLM adapters."""

from __future__ import annotations

from contextbridge.core.llm_interface import LLMInterface


def get_adapter(name: str, **kwargs) -> LLMInterface:
    """
    Factory function to instantiate an LLM adapter by name.

    Parameters
    ----------
    name : str
        Adapter name: 'openai', 'claude', or 'local'.
    **kwargs
        Passed through to the adapter constructor (e.g. api_key, model).

    Returns
    -------
    LLMInterface
        An initialised adapter instance.

    Raises
    ------
    ValueError
        If the adapter name is not recognised.
    """
    name = name.lower().strip()

    if name in ("openai", "gpt"):
        from contextbridge.adapters.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(**kwargs)

    if name in ("claude", "anthropic"):
        from contextbridge.adapters.claude_adapter import ClaudeAdapter

        return ClaudeAdapter(**kwargs)

    if name in ("local", "llama", "ollama"):
        from contextbridge.adapters.local_adapter import LocalAdapter

        return LocalAdapter(**kwargs)

    raise ValueError(f"Unknown adapter '{name}'. Choose from: openai, claude, local")


__all__ = ["get_adapter"]
