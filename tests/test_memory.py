"""Tests for the Memory Extractor."""

import pytest

from contextbridge.core.memory import MemoryExtractor, parse_chat_transcript
from tests.conftest import SAMPLE_CHAT, SAMPLE_EXTRACTION_RESPONSE

# ---------------------------------------------------------------------------
# Chat parsing
# ---------------------------------------------------------------------------


class TestParseTranscript:
    def test_plain_text_format(self):
        transcript = parse_chat_transcript(SAMPLE_CHAT, source_model="gpt")
        assert len(transcript.messages) >= 4
        assert transcript.messages[0].role == "user"
        assert "Tirth" in transcript.messages[0].content
        assert transcript.source_model == "gpt"

    def test_json_format(self):
        import json

        data = json.dumps(
            [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ]
        )
        transcript = parse_chat_transcript(data)
        assert len(transcript.messages) == 2
        assert transcript.messages[0].role == "user"
        assert transcript.messages[1].role == "assistant"

    def test_fallback_single_message(self):
        transcript = parse_chat_transcript("Just some random text")
        assert len(transcript.messages) == 1
        assert transcript.messages[0].role == "user"

    def test_token_estimate(self):
        transcript = parse_chat_transcript(SAMPLE_CHAT)
        assert transcript.total_tokens_estimate > 0


# ---------------------------------------------------------------------------
# Memory extraction
# ---------------------------------------------------------------------------


class TestMemoryExtractor:
    @pytest.mark.asyncio
    async def test_extract_structured_memory(self, mock_adapter):
        mock_adapter.set_response(SAMPLE_EXTRACTION_RESPONSE)
        extractor = MemoryExtractor()
        memory = await extractor.extract(SAMPLE_CHAT, mock_adapter)

        assert len(memory.identity) >= 1
        assert len(memory.projects) >= 1
        assert len(memory.facts) >= 1
        assert len(memory.decisions) >= 1
        assert len(memory.open_tasks) >= 1
        assert len(memory.preferences) >= 1
        assert memory.total_items >= 7

    @pytest.mark.asyncio
    async def test_extract_handles_malformed_json(self, mock_adapter):
        mock_adapter.set_response("not valid json at all")
        extractor = MemoryExtractor()
        memory = await extractor.extract("some chat", mock_adapter)

        # Should return empty memory, not crash
        assert memory.total_items == 0

    @pytest.mark.asyncio
    async def test_extract_handles_markdown_fences(self, mock_adapter):
        mock_adapter.set_response(f"```json\n{SAMPLE_EXTRACTION_RESPONSE}\n```")
        extractor = MemoryExtractor()
        memory = await extractor.extract(SAMPLE_CHAT, mock_adapter)

        assert memory.total_items >= 7

    @pytest.mark.asyncio
    async def test_incremental_extraction(self, mock_adapter, sample_memory):
        mock_adapter.set_response(SAMPLE_EXTRACTION_RESPONSE)
        extractor = MemoryExtractor()

        # Incremental should merge and deduplicate
        merged = await extractor.extract_incremental(SAMPLE_CHAT, sample_memory, mock_adapter)
        # Should not have duplicates
        contents = [item.content for item in merged.all_items]
        assert len(contents) == len(set(contents))
