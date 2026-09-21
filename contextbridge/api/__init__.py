"""FastAPI surface for ContextBridge.

``create_app()`` builds the production HTTP server: the same JSON API the
Flask dashboard exposes (so the dashboard and the Chrome extension keep
working unchanged), versioned under ``/api/v1`` with OpenAPI docs, optional
API-key authentication, readiness / liveness probes for orchestrators, and
OpenTelemetry HTTP spans.

Run it with ``cb serve`` or ``uvicorn contextbridge.api.app:app``.
"""

from contextbridge.api.app import create_app

__all__ = ["create_app"]
