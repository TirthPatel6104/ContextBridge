"""Portable package files — export to / import from a single JSON document.

The export format is a small envelope around the package so that readers can
recognise the file type and schema version without guessing::

    {
      "format": "contextbridge-package",
      "schema_version": 2,
      "exported_at": "...",
      "package": { ...ContextPackage... }
    }

Bare ``ContextPackage`` documents (the legacy ``context_vN.json`` files) are
also accepted on import.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from contextbridge.models import ContextPackage, PackageEnvelope
from contextbridge.validation import ValidationError, validate_package_name

PACKAGE_FORMAT = "contextbridge-package"
MAX_IMPORT_BYTES = 20 * 1024 * 1024


def export_package(package: ContextPackage) -> PackageEnvelope:
    """Wrap a package in the portable envelope."""
    return PackageEnvelope(package=package)


def dumps_package(package: ContextPackage, *, indent: int | None = 2) -> str:
    """Serialise a package as a portable JSON string."""
    return export_package(package).model_dump_json(indent=indent)


def import_package(
    data: str | bytes | dict[str, Any],
    *,
    rename: str | None = None,
    max_bytes: int = MAX_IMPORT_BYTES,
) -> ContextPackage:
    """Parse a portable package file (envelope or bare package).

    Parameters
    ----------
    data
        JSON text / bytes, or an already-parsed dict.
    rename
        Optional new name for the imported package.
    max_bytes
        Reject inputs larger than this before parsing.

    Raises
    ------
    ValidationError
        If the document is not a recognisable package or the name is invalid.
    """
    if isinstance(data, (str, bytes)):
        if len(data) > max_bytes:
            raise ValidationError(
                f"Package file is too large (limit {max_bytes // (1024 * 1024)} MB)"
            )
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"Package file is not valid JSON: {exc}") from exc
    else:
        parsed = data

    if not isinstance(parsed, dict):
        raise ValidationError("Package file must contain a JSON object")

    try:
        if parsed.get("format") == PACKAGE_FORMAT or "package" in parsed:
            envelope = PackageEnvelope.model_validate(parsed)
            package = envelope.package
        elif "name" in parsed and "memory" in parsed:
            package = ContextPackage.model_validate(parsed)
        else:
            raise ValidationError(
                "Unrecognised file: expected a ContextBridge package export "
                "(with a 'package' key) or a bare package document"
            )
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "document"
        raise ValidationError(
            f"Package file failed validation at '{loc}': {first.get('msg')}"
        ) from exc

    if rename:
        package.name = validate_package_name(rename)
    else:
        package.name = validate_package_name(package.name)
    return package
