"""FastAPI application factory.

Routes are served twice: under ``/api/v1`` (documented in OpenAPI at
``/docs``) and under the unversioned ``/api`` prefix that the dashboard and
the Chrome extension already use.  ``/livez`` and ``/readyz`` are unauthenticated
probes for load balancers and orchestrators; everything under ``/api`` honours
``CB_API_KEY`` when it is set.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from contextbridge import __version__, telemetry
from contextbridge.api.deps import require_api_key
from contextbridge.api.routes import api_router
from contextbridge.config import Settings
from contextbridge.service import ContextBridgeService
from contextbridge.storage import get_store
from contextbridge.storage.base import PackageNotFoundError, StorageBackend
from contextbridge.validation import ValidationError

logger = logging.getLogger(__name__)

_WEB = Path(__file__).resolve().parent.parent / "web"
_TEMPLATES = _WEB / "templates"
_STATIC = _WEB / "static"


def _render_index() -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(["html"])
    )
    return env.get_template("index.html").render(version=__version__)


def create_app(
    settings: Settings | None = None,
    *,
    store: StorageBackend | None = None,
    service: ContextBridgeService | None = None,
    configure_tracing: bool = True,
) -> FastAPI:
    """Build the ASGI application.

    Tests inject a temporary ``store`` / ``service``; production lets the
    factory read ``Settings.from_env()`` and open the configured backend.
    """
    settings = settings or Settings.from_env()
    if configure_tracing:
        telemetry.configure_from_settings(settings)
    owns_store = store is None and service is None
    store = store or (service.store if service else get_store(settings=settings))
    svc = service or ContextBridgeService(store, settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "ContextBridge API %s starting (backend=%s, env=%s, tracing=%s)",
            __version__,
            store.backend_name,
            settings.environment,
            telemetry.enabled(),
        )
        try:
            yield
        finally:
            if owns_store:
                store.close()
            telemetry.shutdown()

    app = FastAPI(
        title="ContextBridge API",
        version=__version__,
        description=(
            "Portable, privacy-conscious working memory for moving work between "
            "ChatGPT, Claude, and local models. Extract memory from transcripts, "
            "review and redact it, retrieve what the next task needs, and render a "
            "paste-ready prompt — with sharing boundaries and an egress ledger."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.service = svc
    app.state.store = store

    # -- CORS: only the chat sites the extension runs on -------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "Authorization"],
        max_age=600,
    )

    # -- Upload / body size limit ------------------------------------------
    limit = settings.max_upload_bytes

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse(
                {"error": f"Upload too large. The limit is {limit // (1024 * 1024)} MB."},
                status_code=413,
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # -- Error handling: always ``{"error": ...}`` ---------------------------

    @app.exception_handler(ValidationError)
    async def _bad_request(request: Request, exc: ValidationError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(PackageNotFoundError)
    async def _not_found(request: Request, exc: PackageNotFoundError):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = first.get("msg", "Invalid request")
        return JSONResponse({"error": f"{loc}: {msg}" if loc else msg}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        headers = getattr(exc, "headers", None) or None
        return JSONResponse(
            {"error": str(exc.detail or exc.__class__.__name__)},
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception):
        # Never echo internals (which may include memory content) to the client.
        logger.exception("Unhandled error in %s", request.url.path)
        return JSONResponse(
            {"error": "Internal error. Check the server log for details."}, status_code=500
        )

    # -- Probes (unauthenticated) --------------------------------------------

    @app.get("/livez", include_in_schema=False)
    async def livez():
        return {"ok": True, "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        ping = getattr(store, "ping", None)
        ok = bool(ping()) if callable(ping) else True
        if not ok:
            raise HTTPException(status_code=503, detail="storage unavailable")
        return {"ready": True, "storage_backend": store.backend_name}

    # -- API: versioned + compatibility prefix --------------------------------
    guarded = [Depends(require_api_key)]
    app.include_router(api_router(), prefix="/api/v1", dependencies=guarded)
    app.include_router(api_router(), prefix="/api", dependencies=guarded, include_in_schema=False)

    # -- Dashboard -----------------------------------------------------------
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_render_index())

    if telemetry.enabled():
        telemetry.instrument_fastapi(app)
    return app


def app_factory() -> FastAPI:  # pragma: no cover - uvicorn entry point
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=os.getenv("CB_LOG_LEVEL", "INFO"))
    return create_app()


def run(host: str | None = None, port: int | None = None, *, reload: bool = False) -> None:
    """Start uvicorn with the configured host / port (``cb serve``)."""  # pragma: no cover
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "contextbridge.api.app:app_factory",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=os.getenv("CB_LOG_LEVEL", "info").lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":  # pragma: no cover
    run()
