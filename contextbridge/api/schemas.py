"""Request and response models for the FastAPI surface.

Request bodies are strict (unknown fields are ignored, types are validated)
so that the OpenAPI document is an accurate contract.  Responses that carry
whole packages or retrieval results are documented as loose objects because
their shape is owned by :class:`~contextbridge.service.ContextBridgeService`
and shared with the CLI and dashboard.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    error: str


class HealthResponse(BaseModel):
    ok: bool = True
    version: str
    environment: str
    storage_backend: str
    storage_location: str
    redact_by_default: bool
    redaction_kinds: list[dict[str, str]]
    categories: list[str]
    sharing_policies: list[str]
    item_statuses: list[str]
    max_upload_mb: int
    api_key_required: bool
    tracing: bool


class ReadinessResponse(BaseModel):
    ready: bool
    storage_backend: str
    checks: dict[str, bool]


class PackageSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    version: int
    source_model: str
    total_items: int
    created_at: str
    updated_at: str
    schema_version: int = 1


class PackageListResponse(BaseModel):
    packages: list[PackageSummary]


class RetrievalOptionsBody(BaseModel):
    """Optional retrieval knobs accepted by ``/retrieve`` and ``/prompt``."""

    top_k: int | None = Field(default=None, ge=1, le=100)
    categories: list[str] | str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    include_inactive: bool | None = None


class RetrieveRequest(RetrievalOptionsBody):
    package_name: str
    query: str = ""


class PromptRequest(RetrievalOptionsBody):
    package_name: str
    target_model: str = "claude"
    query: str | None = None
    include_files: bool = False
    include_watch: bool = False
    surface: str = Field(default="api", max_length=32)
    record: bool = True


class ScanRequest(BaseModel):
    text: str


class IdsRequest(BaseModel):
    ids: list[str]
    note: str = ""


class SharingRequest(BaseModel):
    ids: list[str]
    policy: str


class StatusRequest(BaseModel):
    ids: list[str]
    status: str


class SupersedeRequest(BaseModel):
    old_id: str
    new_id: str


class ResolveConflictRequest(BaseModel):
    existing_id: str
    incoming_id: str
    action: str


class RollbackRequest(BaseModel):
    version: int


class MergeRequest(BaseModel):
    names: list[str]
    new_name: str
    dry_run: bool = False
    overwrite: bool = False


class ImportPackageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    package_file: dict[str, Any] | None = None
    rename: str | None = None
    overwrite: bool = False


class SimpleSuccess(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = True
