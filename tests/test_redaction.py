"""Tests for secrets / PII detection and redaction."""

from __future__ import annotations

import pytest

from contextbridge.core.redaction import (
    DETECTOR_KINDS,
    RedactionPolicy,
    placeholder,
    redact_item,
    redact_memory,
    redact_text,
    scan_text,
)
from contextbridge.models import MemoryCategory, MemoryItem, StructuredMemory

SECRET_KEY = "sk-proj-abc123def456ghi789jkl012mno345pqr678"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("mail me at jane.doe@example.com", "email"),
        ("call +1 415-555-0142 tomorrow", "phone"),
        (f"key is {SECRET_KEY}", "api_key"),
        ("ghp_1234567890abcdefghijklmnopqrstuvwxyzAB", "api_key"),
        ("AKIAIOSFODNN7EXAMPLE", "api_key"),
        ("xoxb-1234567890-abcdefghijkl-mnopqrstuvwx", "api_key"),
        (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "jwt",
        ),
        ("card 4111 1111 1111 1111", "credit_card"),
        ("ssn 123-45-6789", "ssn"),
        ("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", "private_key"),
        ("password = hunter2hunter2", "credential_assignment"),
        ("postgres://app:S3cretPassw0rd@db.internal/ledger", "credential_url"),
        ("box at 10.20.30.40", "ip_address"),
    ],
)
def test_detects_each_kind(text, kind):
    kinds = {f.kind for f in scan_text(text)}
    assert kind in kinds


@pytest.mark.parametrize(
    "text",
    [
        "Python 3.11.4 and pydantic 2.5.0",
        "launch on 2024-10-14",
        "order #12345678",
        "port 8080 returns 200",
        "version v1.2.3",
        "meeting at 10:30 in room 204",
        "coverage went from 71% to 88%",
    ],
)
def test_clean_text_not_flagged(text):
    assert scan_text(text) == []


def test_redact_text_replaces_with_placeholders():
    redacted, findings = redact_text(f"Key {SECRET_KEY} and mail a@b.io")
    assert SECRET_KEY not in redacted
    assert "a@b.io" not in redacted
    assert placeholder("api_key") in redacted and placeholder("email") in redacted
    assert [f.kind for f in findings] == ["api_key", "email"]
    # Previews never reveal the whole value
    for f in findings:
        assert SECRET_KEY not in f.preview and "a@b.io" != f.preview


def test_credential_assignment_keeps_key_name():
    redacted, findings = redact_text("api_key: 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c")
    assert redacted.startswith("api_key: [REDACTED:")
    assert findings[0].kind == "credential_assignment"


def test_policy_disabled_and_subset():
    text = f"{SECRET_KEY} a@b.io"
    assert scan_text(text, RedactionPolicy.disabled()) == []
    only_email = scan_text(text, RedactionPolicy(kinds=["email"]))
    assert [f.kind for f in only_email] == ["email"]


def test_redact_item_recomputes_id_and_records():
    item = MemoryItem(
        category=MemoryCategory.FACTS,
        content=f"Deploy key is {SECRET_KEY}",
        source=f"...{SECRET_KEY}...",
    )
    old_id = item.id
    updated, findings = redact_item(item)
    assert len(findings) == 2  # content + source
    assert updated.id != old_id
    assert updated.was_redacted
    assert updated.redactions[0].kind == "api_key" and updated.redactions[0].count == 2
    assert SECRET_KEY not in updated.content and SECRET_KEY not in updated.source
    # original object untouched
    assert SECRET_KEY in item.content


def test_redact_memory_report():
    memory = StructuredMemory.from_items(
        [
            MemoryItem(category=MemoryCategory.IDENTITY, content="Email is jane@example.com"),
            MemoryItem(category=MemoryCategory.DECISIONS, content="Use PostgreSQL"),
        ]
    )
    redacted, report = redact_memory(memory)
    assert report.items_total == 2 and report.items_redacted == 1
    assert report.counts == {"email": 1}
    assert report.summary()["labels"]["email"] == "Email address"
    assert redacted.decisions[0].content == "Use PostgreSQL"
    assert "[REDACTED:EMAIL]" in redacted.identity[0].content


def test_redact_memory_disabled_returns_same_content():
    memory = StructuredMemory.from_items(
        [MemoryItem(category=MemoryCategory.IDENTITY, content="jane@example.com")]
    )
    same, report = redact_memory(memory, RedactionPolicy.disabled())
    assert same.identity[0].content == "jane@example.com"
    assert report.total == 0


def test_all_kinds_have_labels():
    from contextbridge.core.redaction import DETECTOR_LABELS

    assert set(DETECTOR_KINDS) == set(DETECTOR_LABELS)
