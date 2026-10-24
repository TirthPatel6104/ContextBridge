"""OpenTelemetry tracing for ContextBridge.

Tracing is **optional** and **content-free**:

* If the ``opentelemetry`` packages are not installed, or tracing is not
  enabled, every helper here is a no-op and the rest of the code base does
  not change behaviour.
* Span attributes carry package names, counts, engine / target names, and
  timings — never transcript text, memory content, prompts, or keys.

Configuration (all optional):

``CB_OTEL_ENABLED``            ``true`` to export traces (implied when an OTLP
                              endpoint is configured)
``OTEL_EXPORTER_OTLP_ENDPOINT`` e.g. ``http://otel-collector:4318`` (OTLP/HTTP)
``OTEL_SERVICE_NAME``         defaults to ``contextbridge``
``CB_OTEL_CONSOLE``           ``true`` to also print spans to stdout

Usage::

    from contextbridge.telemetry import span, traced

    with span("retrieve", package=name, top_k=5) as s:
        ...
        s.set_attribute("cb.selected", 3)

    @traced("import_text")
    def import_text(self, ...): ...
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from contextbridge import __version__

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

_ATTR_PREFIX = "cb."
_configured = False
_provider: Any = None

try:  # pragma: no cover - exercised only when the SDK is installed
    from opentelemetry import trace as _otel_trace

    _OTEL_API_AVAILABLE = True
except Exception:  # pragma: no cover - SDK missing
    _otel_trace = None
    _OTEL_API_AVAILABLE = False


class _NoopSpan:
    """Stand-in for an OpenTelemetry span when tracing is unavailable."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        return None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def is_recording(self) -> bool:
        return False


_NOOP = _NoopSpan()


def available() -> bool:
    """Whether the OpenTelemetry API is importable."""
    return _OTEL_API_AVAILABLE


def enabled() -> bool:
    """Whether a real tracer provider has been configured by :func:`configure`."""
    return _configured


def _clean_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Prefix attribute keys and drop values OpenTelemetry cannot encode."""
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, bool | int | float | str):
            cleaned[f"{_ATTR_PREFIX}{key}"] = value
        elif isinstance(value, list | tuple) and all(
            isinstance(v, bool | int | float | str) for v in value
        ):
            cleaned[f"{_ATTR_PREFIX}{key}"] = list(value)
        else:
            cleaned[f"{_ATTR_PREFIX}{key}"] = str(value)[:200]
    return cleaned


def configure(
    *,
    service_name: str = "contextbridge",
    endpoint: str | None = None,
    console: bool = False,
    environment: str = "development",
    span_processor: Any | None = None,
    force: bool = False,
) -> bool:
    """Install a tracer provider.

    Returns ``True`` when tracing is active.  Safe to call more than once; the
    second call is a no-op unless ``force`` is set (tests use that to swap in
    an in-memory exporter).
    """
    global _configured, _provider
    if not _OTEL_API_AVAILABLE:
        logger.info("OpenTelemetry SDK not installed — tracing disabled")
        return False
    if _configured and not force:
        return True
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
    except Exception:  # pragma: no cover - api without sdk
        logger.info("OpenTelemetry SDK not installed — tracing disabled")
        return False

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": __version__,
            "deployment.environment": environment,
        }
    )
    provider = TracerProvider(resource=resource)
    exporters = 0
    if span_processor is not None:
        provider.add_span_processor(span_processor)
        exporters += 1
    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            traces_url = endpoint.rstrip("/")
            if not traces_url.endswith("/v1/traces"):
                traces_url += "/v1/traces"
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_url)))
            exporters += 1
        except Exception as exc:  # pragma: no cover - exporter missing
            logger.warning("OTLP exporter unavailable (%s)", exc.__class__.__name__)
    if console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stdout)))
        exporters += 1
    if exporters == 0:
        logger.info("Tracing requested but no exporter configured — spans are recorded locally")

    if _provider is not None and force:
        with contextlib.suppress(Exception):
            _provider.shutdown()
    _otel_trace.set_tracer_provider(provider)
    # ``set_tracer_provider`` refuses to override an existing global; keep our
    # own handle so :func:`tracer` always uses the latest provider.
    _provider = provider
    _configured = True
    logger.info("OpenTelemetry tracing enabled (service=%s)", service_name)
    return True


def configure_from_settings(settings: Any) -> bool:
    """Convenience: enable tracing according to a :class:`Settings` object."""
    if not getattr(settings, "otel_enabled", False):
        return False
    return configure(
        service_name=getattr(settings, "otel_service_name", "contextbridge"),
        console=getattr(settings, "otel_console", False),
        environment=getattr(settings, "environment", "development"),
    )


def shutdown() -> None:
    """Flush exporters (call at process exit)."""
    global _configured, _provider
    if _provider is not None:
        with contextlib.suppress(Exception):
            _provider.force_flush()
        with contextlib.suppress(Exception):
            _provider.shutdown()
    _provider = None
    _configured = False


def tracer() -> Any:
    if not _configured or _provider is None:
        return None
    return _provider.get_tracer("contextbridge", __version__)


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span named ``cb.<name>`` with content-free attributes.

    Yields the span (or a no-op stand-in).  Exceptions are recorded and
    re-raised.
    """
    t = tracer()
    if t is None:
        yield _NOOP
        return
    with t.start_as_current_span(f"cb.{name}") as s:
        attrs = _clean_attributes(attributes)
        if attrs:
            s.set_attributes(attrs)
        try:
            yield s
        except Exception as exc:
            with contextlib.suppress(Exception):
                s.record_exception(exc)
                from opentelemetry.trace import Status, StatusCode

                s.set_status(Status(StatusCode.ERROR, exc.__class__.__name__))
            raise


def traced(name: str | None = None, **static_attributes: Any) -> Callable[[F], F]:
    """Decorate a function so each call runs inside a span.

    Keyword arguments named ``package_name``, ``name``, ``target_model``,
    ``engine`` and ``surface`` are copied to the span when present; positional
    arguments are ignored so content can never leak by accident.
    """

    def decorator(func: F) -> F:
        span_name = name or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _configured:
                return func(*args, **kwargs)
            attrs = dict(static_attributes)
            for key in ("package_name", "name", "target_model", "engine", "surface", "mode"):
                value = kwargs.get(key)
                if isinstance(value, str):
                    attrs[key if key != "name" else "package_name"] = value
            with span(span_name, **attrs):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def set_attribute(key: str, value: Any) -> None:
    """Set an attribute on the current span, if any."""
    if not _configured or _otel_trace is None:
        return
    current = _otel_trace.get_current_span()
    if current is None or not current.is_recording():
        return
    for k, v in _clean_attributes({key: value}).items():
        current.set_attribute(k, v)


def instrument_fastapi(app: Any) -> bool:
    """Attach automatic HTTP server spans to a FastAPI app (if instrumentation is installed)."""
    if not _configured:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except Exception:
        logger.info("opentelemetry-instrumentation-fastapi not installed — HTTP spans disabled")
        return False
    try:
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=_provider, excluded_urls="livez,readyz,static"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not instrument FastAPI app (%s)", exc.__class__.__name__)
        return False
    return True


__all__ = [
    "available",
    "enabled",
    "configure",
    "configure_from_settings",
    "shutdown",
    "span",
    "traced",
    "set_attribute",
    "instrument_fastapi",
]
