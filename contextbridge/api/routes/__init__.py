"""Routers for the FastAPI surface."""

from fastapi import APIRouter

from contextbridge.api.routes import files, memory, meta, packages


def api_router() -> APIRouter:
    """All ``/api`` routes (meta, memory workflow, packages, attached files)."""
    router = APIRouter()
    router.include_router(meta.router)
    router.include_router(memory.router)
    router.include_router(packages.router)
    router.include_router(files.router)
    return router


__all__ = ["api_router"]
