"""Tests for validation helpers, token estimates, file parsing, and settings."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from contextbridge.config import Settings
from contextbridge.core.file_parser import parse_file, parse_multiple_files
from contextbridge.core.tokens import estimate_tokens
from contextbridge.validation import (
    ValidationError,
    safe_filename,
    suggest_package_name,
    validate_package_name,
    validate_upload_filename,
)


class TestValidation:
    @pytest.mark.parametrize("name", ["proj", "my-project.v2", "A1", "x" * 64])
    def test_accepts_good_names(self, name):
        assert validate_package_name(f"  {name} ") == name

    @pytest.mark.parametrize("name", [None, "", "..", "a/b", "a\\b", ".hidden", "-x", "x" * 65])
    def test_rejects_bad_names(self, name):
        with pytest.raises(ValidationError):
            validate_package_name(name)

    def test_suggest_and_safe_filename(self):
        assert suggest_package_name("My Chat: Design Review!") == "my_chat_design_review"
        assert suggest_package_name("!!!") == "imported_context"
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename("C:\\Users\\me\\notes.md") == "notes.md"
        with pytest.raises(ValidationError):
            safe_filename("..")

    def test_upload_filename(self):
        assert validate_upload_filename("Chat Export.ZIP") == "Chat Export.ZIP"
        with pytest.raises(ValidationError):
            validate_upload_filename("script.py")
        with pytest.raises(ValidationError):
            validate_upload_filename("noext")


class TestTokens:
    def test_estimates(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("   ") == 0
        short = estimate_tokens("Use PostgreSQL for production")
        assert 4 <= short <= 10
        assert estimate_tokens("word " * 400) > estimate_tokens("word " * 100)


class TestFileParser:
    def test_chatgpt_export_zip(self):
        conversations = [
            {
                "title": "Design chat",
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["m1"], "message": None},
                    "m1": {
                        "id": "m1",
                        "parent": "root",
                        "children": ["m2"],
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["I decided to use Postgres."]},
                        },
                    },
                    "m2": {
                        "id": "m2",
                        "parent": "m1",
                        "children": [],
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Great choice."]},
                        },
                    },
                },
            }
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("conversations.json", json.dumps(conversations))
        text = parse_file(buf.getvalue(), "export.zip")
        assert "Design chat" in text
        assert "User: I decided to use Postgres." in text
        assert "Assistant: Great choice." in text

    def test_json_messages_and_html(self):
        msgs = json.dumps(
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        )
        assert "User: hi" in parse_file(msgs.encode(), "chat.json")
        html = b"<html><style>x{}</style><body><p>User: hello &amp; bye</p></body></html>"
        text = parse_file(html, "page.html")
        assert "User: hello & bye" in text and "x{}" not in text

    def test_multiple_files_labelled(self):
        text = parse_multiple_files([(b"User: a", "one.txt"), (b"User: b", "two.md")])
        assert "--- File: one.txt ---" in text and "--- File: two.md ---" in text


class TestSettings:
    def test_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CB_STORAGE_DIR", str(tmp_path))
        monkeypatch.setenv("CB_STORAGE_BACKEND", "json")
        monkeypatch.setenv("CB_REDACT", "off")
        monkeypatch.setenv("CB_ALLOWED_ORIGINS", "https://a.example, https://b.example")
        monkeypatch.setenv("CB_MAX_UPLOAD_MB", "3")
        s = Settings.from_env()
        assert s.storage_dir == tmp_path and s.storage_backend == "json"
        assert s.redact_by_default is False
        assert s.allowed_origins == ("https://a.example", "https://b.example")
        assert s.max_upload_bytes == 3 * 1024 * 1024
        assert s.host == "127.0.0.1"

    def test_defaults_are_safe(self):
        s = Settings()
        assert s.storage_backend == "sqlite" and s.redact_by_default
        assert "https://claude.ai" in s.allowed_origins
        assert s.host == "127.0.0.1"
