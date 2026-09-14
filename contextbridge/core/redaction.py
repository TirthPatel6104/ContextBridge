"""
Secrets and PII detection / redaction.

The redactor runs *before* memory is stored or exported.  It is deliberately
conservative and fully transparent:

* Every detector has a name (``kind``) that is reported back to the user.
* Redaction replaces the sensitive span with ``[REDACTED:<KIND>]`` and records
  *what kind* and *how many* were masked on the memory item — never the value.
* Original inputs on disk are never modified; only the extracted memory that
  ContextBridge itself writes is affected.  Users can disable redaction per
  import or per detector kind when they knowingly want the originals kept.

Detection is regex-based (plus a Luhn check for card numbers).  It will miss
unusual formats and can produce false positives on things like version
strings; the review step in the dashboard exists precisely so a human can see
what was flagged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from contextbridge.models import (
    MemoryCategory,
    MemoryItem,
    RedactionRecord,
    StructuredMemory,
    make_item_id,
)

# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detector:
    kind: str
    label: str
    pattern: re.Pattern[str]
    #: Regex group whose span is redacted (0 = whole match).
    group: int = 0
    #: Optional extra validation on the matched value.
    validate: object | None = None
    #: Lower runs first and wins overlaps.
    priority: int = 50


def _luhn_ok(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _phone_ok(value: str) -> bool:
    digits = sum(c.isdigit() for c in value)
    return 10 <= digits <= 15


def _ip_ok(value: str) -> bool:
    parts = value.split(".")
    if any(int(p) > 255 for p in parts):
        return False
    return value not in {"0.0.0.0", "127.0.0.1", "255.255.255.255"}


DETECTORS: tuple[Detector, ...] = (
    Detector(
        kind="private_key",
        label="Private key block",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        priority=1,
    ),
    Detector(
        kind="api_key",
        label="API key / access token",
        pattern=re.compile(
            r"(?<![A-Za-z0-9_\-])("
            r"sk-ant-[A-Za-z0-9_\-]{20,}"
            r"|sk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"
            r"|ghp_[A-Za-z0-9]{30,}"
            r"|github_pat_[A-Za-z0-9_]{22,}"
            r"|gho_[A-Za-z0-9]{30,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|xox[abprs]-[A-Za-z0-9\-]{10,}"
            r"|AIza[0-9A-Za-z\-_]{35}"
            r"|gsk_[A-Za-z0-9]{20,}"
            r"|hf_[A-Za-z0-9]{30,}"
            r"|pk_(?:live|test)_[A-Za-z0-9]{20,}"
            r"|rk_(?:live|test)_[A-Za-z0-9]{20,}"
            r")(?![A-Za-z0-9_\-])"
        ),
        priority=2,
    ),
    Detector(
        kind="jwt",
        label="JSON Web Token",
        pattern=re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
        priority=3,
    ),
    Detector(
        kind="credential_assignment",
        label="Password / secret assignment",
        pattern=re.compile(
            r"(?i)\b(?:api[_\- ]?key|secret(?:[_\- ]?key)?|access[_\- ]?token|auth[_\- ]?token"
            r"|token|password|passwd|pwd|client[_\- ]?secret)\b\s*(?:[:=]|is)\s*"
            r"[\"'`]?([^\s\"'`,;]{8,})"
        ),
        group=1,
        priority=4,
    ),
    Detector(
        kind="credential_url",
        label="URL with embedded credentials",
        pattern=re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:([^\s/@]+)@"),
        group=1,
        priority=5,
    ),
    Detector(
        kind="credit_card",
        label="Payment card number",
        pattern=re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)"),
        validate=_luhn_ok,
        priority=10,
    ),
    Detector(
        kind="ssn",
        label="US Social Security number",
        pattern=re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
        priority=11,
    ),
    Detector(
        kind="email",
        label="Email address",
        pattern=re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        priority=20,
    ),
    Detector(
        kind="phone",
        label="Phone number",
        pattern=re.compile(
            r"(?<![\w.])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.\-]\d{3,4}[\s.\-]\d{3,4}"
            r"(?!\w)"
        ),
        validate=_phone_ok,
        priority=30,
    ),
    Detector(
        kind="ip_address",
        label="IP address",
        pattern=re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
        validate=_ip_ok,
        priority=40,
    ),
)

DETECTOR_KINDS: tuple[str, ...] = tuple(d.kind for d in DETECTORS)
DETECTOR_LABELS: dict[str, str] = {d.kind: d.label for d in DETECTORS}


# ---------------------------------------------------------------------------
# Policy / results
# ---------------------------------------------------------------------------


class RedactionPolicy(BaseModel):
    """Which detectors run.  ``kinds=None`` means all of them."""

    enabled: bool = True
    kinds: list[str] | None = Field(default=None)

    def active_detectors(self) -> list[Detector]:
        if not self.enabled:
            return []
        if self.kinds is None:
            return list(DETECTORS)
        wanted = set(self.kinds)
        return [d for d in DETECTORS if d.kind in wanted]

    @classmethod
    def disabled(cls) -> RedactionPolicy:
        return cls(enabled=False)


class RedactionFinding(BaseModel):
    """One masked span.  ``preview`` is a partially masked hint for review."""

    kind: str
    label: str
    start: int
    end: int
    preview: str


class RedactionReport(BaseModel):
    """Aggregate result of redacting a memory or a text."""

    findings: list[RedactionFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    items_redacted: int = 0
    items_total: int = 0

    @property
    def total(self) -> int:
        return len(self.findings)

    def summary(self) -> dict[str, object]:
        return {
            "total": self.total,
            "counts": dict(self.counts),
            "labels": {k: DETECTOR_LABELS.get(k, k) for k in self.counts},
            "items_redacted": self.items_redacted,
            "items_total": self.items_total,
        }


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def _preview(value: str) -> str:
    """Partially mask a value so a reviewer can recognise it without seeing it."""
    v = value.strip()
    if "@" in v and " " not in v:
        local, _, domain = v.partition("@")
        return f"{local[:1]}***@{domain}"
    if len(v) <= 6:
        return "*" * len(v)
    return f"{v[:3]}…{v[-2:]}"


def placeholder(kind: str) -> str:
    return f"[REDACTED:{kind.upper()}]"


def scan_text(text: str, policy: RedactionPolicy | None = None) -> list[RedactionFinding]:
    """Find sensitive spans in *text* without modifying it."""
    policy = policy or RedactionPolicy()
    if not text:
        return []
    taken: list[tuple[int, int]] = []
    findings: list[RedactionFinding] = []
    for det in sorted(policy.active_detectors(), key=lambda d: d.priority):
        for match in det.pattern.finditer(text):
            start, end = match.span(det.group)
            if start == end:
                continue
            value = text[start:end]
            if det.validate is not None and not det.validate(value):  # type: ignore[operator]
                continue
            if any(s < end and start < e for s, e in taken):
                continue
            taken.append((start, end))
            findings.append(
                RedactionFinding(
                    kind=det.kind,
                    label=det.label,
                    start=start,
                    end=end,
                    preview=_preview(value),
                )
            )
    findings.sort(key=lambda f: f.start)
    return findings


def redact_text(
    text: str, policy: RedactionPolicy | None = None
) -> tuple[str, list[RedactionFinding]]:
    """Return ``(redacted_text, findings)``."""
    findings = scan_text(text, policy)
    if not findings:
        return text, []
    out: list[str] = []
    cursor = 0
    for f in findings:
        out.append(text[cursor : f.start])
        out.append(placeholder(f.kind))
        cursor = f.end
    out.append(text[cursor:])
    return "".join(out), findings


def redact_item(
    item: MemoryItem, policy: RedactionPolicy | None = None
) -> tuple[MemoryItem, list[RedactionFinding]]:
    """Redact one item's content and source excerpt, recomputing its id if changed."""
    new_content, f_content = redact_text(item.content, policy)
    new_source, f_source = redact_text(item.source, policy)
    findings = f_content + f_source
    if not findings:
        return item, []
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    records = list(item.redactions) + [RedactionRecord(kind=k, count=n) for k, n in counts.items()]
    updated = item.model_copy(
        update={
            "content": new_content,
            "source": new_source,
            "redactions": records,
            "id": make_item_id(item.category.value, new_content),
        }
    )
    return updated, findings


def redact_memory(
    memory: StructuredMemory, policy: RedactionPolicy | None = None
) -> tuple[StructuredMemory, RedactionReport]:
    """Redact every item; returns a new memory and a report of what was masked."""
    policy = policy or RedactionPolicy()
    report = RedactionReport(items_total=memory.total_items)
    if not policy.enabled:
        return memory, report
    result = StructuredMemory()
    for cat in MemoryCategory:
        new_items: list[MemoryItem] = []
        for item in memory.get_category(cat):
            updated, findings = redact_item(item, policy)
            if findings:
                report.items_redacted += 1
                report.findings.extend(findings)
                for f in findings:
                    report.counts[f.kind] = report.counts.get(f.kind, 0) + 1
            new_items.append(updated)
        result.set_category(cat, new_items)
    return result, report


def describe_kinds(kinds: Iterable[str] | None = None) -> list[dict[str, str]]:
    """Kinds + labels for UIs that let users toggle detectors."""
    wanted = set(kinds) if kinds is not None else None
    return [
        {"kind": d.kind, "label": d.label} for d in DETECTORS if wanted is None or d.kind in wanted
    ]
