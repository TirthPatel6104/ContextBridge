"""Tests for the Prompt Builder."""

from contextbridge.core.prompt_builder import PromptBuilder


class TestPromptBuilder:
    def test_build_markdown(self, sample_memory):
        builder = PromptBuilder()
        from contextbridge.core.packager import ContextPackager

        pkg = ContextPackager().create("test", sample_memory, source_model="gpt")
        prompt = builder.build(pkg, "openai")

        assert "## User Identity" in prompt
        assert "## Active Projects" in prompt
        assert "Tirth" in prompt
        assert "v1" in prompt

    def test_build_xml(self, sample_memory):
        builder = PromptBuilder()
        from contextbridge.core.packager import ContextPackager

        pkg = ContextPackager().create("test", sample_memory, source_model="gpt")
        prompt = builder.build(pkg, "claude")

        assert "<context>" in prompt
        assert "<identity>" in prompt
        assert "</context>" in prompt

    def test_build_plain(self, sample_memory):
        builder = PromptBuilder()
        from contextbridge.core.packager import ContextPackager

        pkg = ContextPackager().create("test", sample_memory, source_model="gpt")
        prompt = builder.build(pkg, "local")

        assert "[User Identity]" in prompt

    def test_empty_memory(self):
        builder = PromptBuilder()
        from contextbridge.core.packager import ContextPackager
        from contextbridge.models import StructuredMemory

        pkg = ContextPackager().create("empty", StructuredMemory())
        prompt = builder.build(pkg, "openai")
        assert prompt == ""

    def test_format_override(self, sample_memory):
        builder = PromptBuilder()
        from contextbridge.core.packager import ContextPackager

        pkg = ContextPackager().create("test", sample_memory)
        prompt = builder.build(pkg, "openai", format_override="xml")
        assert "<context>" in prompt

    def test_build_from_memory(self, sample_memory):
        builder = PromptBuilder()
        prompt = builder.build_from_memory(sample_memory, "openai")
        assert "##" in prompt
