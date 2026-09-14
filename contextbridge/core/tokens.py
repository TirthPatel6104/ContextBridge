"""Token estimation helpers.

ContextBridge never calls a vendor tokenizer, so every number produced here
is an *estimate*.  The heuristic (roughly four characters per token for
English prose, blended with a word count) is within about 15-20% of GPT and
Claude tokenizers on conversational text and is labelled as an estimate
wherever it is shown to users.
"""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 4.0
TOKENS_PER_WORD = 1.3


def estimate_tokens(text: str) -> int:
    """Return an estimated token count for *text*.

    The estimate averages a character-based count with a word-based count so
    that both dense code-like strings and ordinary prose land in a sensible
    range.  Empty or whitespace-only input yields zero.
    """
    if not text:
        return 0
    stripped = text.strip()
    if not stripped:
        return 0
    by_chars = len(stripped) / CHARS_PER_TOKEN
    by_words = len(stripped.split()) * TOKENS_PER_WORD
    return max(1, math.ceil((by_chars + by_words) / 2))


def estimate_tokens_many(texts: list[str]) -> int:
    """Sum of :func:`estimate_tokens` over *texts*."""
    return sum(estimate_tokens(t) for t in texts)
