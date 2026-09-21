"""Request-scoped dependencies and small helpers shared by the routers."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from contextbridge.config import Settings
from contextbridge.models import MemoryCategory, RetrievalOptions
from contextbridge.service import ContextBridgeService
from contextbridge.validation import ValidationError


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_service(request: Request) -> ContextBridgeService:
    return request.app.state.service


SettingsDep = Annotated[Settings, Depends(get_settings)]
ServiceDep = Annotated[ContextBridgeService, Depends(get_service)]


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject the request unless it carries the configured API key.

    A no-op when ``CB_API_KEY`` is unset (the local-first default).  Keys may
    be sent as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
    """
    settings: Settings = request.app.state.settings
    expected = settings.api_key
    if not expected:
        return
    supplied = x_api_key or ""
    if not supplied and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = token.strip()
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid API key is required (X-API-Key header)",
            headers={"WWW-Authenticate": "Bearer"},
        )


def truthy(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_options(payload: dict[str, Any]) -> RetrievalOptions:
    """Build :class:`RetrievalOptions` from a loose JSON body, with friendly errors."""
    kwargs: dict[str, Any] = {}
    if payload.get("top_k") not in (None, ""):
        kwargs["top_k"] = int(payload["top_k"])
    if payload.get("token_budget") not in (None, ""):
        kwargs["token_budget"] = int(payload["token_budget"])
    if payload.get("min_score") not in (None, ""):
        kwargs["min_score"] = float(payload["min_score"])
    if payload.get("include_inactive") not in (None, ""):
        kwargs["include_inactive"] = truthy(payload.get("include_inactive"))
    cats = payload.get("categories")
    if cats:
        if isinstance(cats, str):
            cats = [c for c in cats.split(",") if c.strip()]
        try:
            kwargs["categories"] = [MemoryCategory(str(c).strip()) for c in cats]
        except ValueError as exc:
            raise ValidationError(f"Unknown category: {exc}") from exc
    try:
        return RetrievalOptions(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid retrieval options: {exc}") from exc


def ids_from(payload: dict[str, Any]) -> list[str]:
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        raise ValidationError("'ids' must be a list")
    return [str(i) for i in ids]
