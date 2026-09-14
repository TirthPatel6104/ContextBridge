"""Input validation shared by the CLI and the web app.

Everything that comes from a user — package names, uploaded file names,
pasted text — passes through here before touching the filesystem or the
database.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MAX_PACKAGE_NAME_LENGTH = 64

#: File types the importer knows how to turn into text.
ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".json", ".pdf", ".html", ".htm", ".txt", ".md", ".text", ".log", ".markdown"}
)


class ValidationError(ValueError):
    """Raised when user-supplied input is rejected."""


def validate_package_name(name: str | None) -> str:
    """Return a cleaned package name or raise :class:`ValidationError`.

    Names are restricted to a filesystem- and URL-safe alphabet so that they
    can never escape the storage directory or collide with reserved names.
    """
    if name is None:
        raise ValidationError("Package name is required")
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("Package name is required")
    if len(cleaned) > MAX_PACKAGE_NAME_LENGTH:
        raise ValidationError(f"Package name must be at most {MAX_PACKAGE_NAME_LENGTH} characters")
    if ".." in cleaned:
        raise ValidationError("Package name may not contain '..'")
    if not PACKAGE_NAME_PATTERN.match(cleaned):
        raise ValidationError(
            "Package name may only contain letters, digits, '_', '-' and '.', "
            "and must start with a letter or digit"
        )
    return cleaned


def is_valid_package_name(name: str | None) -> bool:
    try:
        validate_package_name(name)
    except ValidationError:
        return False
    return True


def suggest_package_name(raw: str, fallback: str = "imported_context") -> str:
    """Turn an arbitrary string (file name, chat title) into a valid package name."""
    stem = re.sub(r"[^A-Za-z0-9]+", "_", raw or "").strip("_").lower()
    stem = stem[:MAX_PACKAGE_NAME_LENGTH].strip("_")
    if not stem or not stem[0].isalnum():
        return fallback
    return stem


def safe_filename(filename: str | None) -> str:
    """Strip any directory components and dangerous characters from a file name."""
    if not filename:
        raise ValidationError("File name is required")
    # Handle both separators regardless of the host OS.
    name = PureWindowsPath(PurePosixPath(filename).name).name
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise ValidationError("File name is not valid")
    return name[:128]


def validate_upload_filename(filename: str | None) -> str:
    """Validate an uploaded file name and return the sanitised version."""
    name = safe_filename(filename)
    suffix = "." + name.rsplit(".", 1)[1].lower() if "." in name else ""
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
        raise ValidationError(f"Unsupported file type '{suffix or 'none'}'. Allowed: {allowed}")
    return name


def validate_text_size(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        raise ValidationError(
            f"Input is too large ({len(text):,} characters). The limit is {max_chars:,}."
        )
    return text
