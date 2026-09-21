"""Tests for OpenTelemetry integration (in-memory exporter, no network)."""

from __future__ import annotations

import pytest

from contextbridge import telemetry
from contextbridge.config import Settings
from contextbridge.service import ContextBridgeService
from contextbridge.storage.base import PackageNotFoundError
from contextbridge.storage.sqlite_store import SQLiteStore
from contextbridge.validation import ValidationError
from tests.conftest import SAMPLE_CHAT

otel_sdk = pytest.importorskip("opentelemetry.sdk")


@pytest.fixture
def exporter():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    assert telemetry.configure(
        service_name="cb-test", span_processor=SimpleSpanProcessor(exp), force=True
    )
    yield exp
    telemetry.shutdown()


def _names(exporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def test_noop_when_not_configured():
    telemetry.shutdown()
    assert telemetry.enabled() is False
    with telemetry.span("anything", package_name="p") as s:
        s.set_attribute("x", 1)  # no-op span
    telemetry.set_attribute("x", 1)

    @telemetry.traced("fn")
    def fn(a, *, package_name=""):
        return a * 2

    assert fn(2, package_name="p") == 4
    assert telemetry.instrument_fastapi(object()) is False
    assert telemetry.configure_from_settings(Settings(otel_enabled=False)) is False


def test_spans_carry_counts_never_content(exporter, tmp_path):
    store = SQLiteStore(base_dir=tmp_path)
    svc = ContextBridgeService(store, settings=Settings(storage_dir=tmp_path))
    try:
        svc.import_text(SAMPLE_CHAT, "proj", origin="chat.txt")
        svc.retrieve("proj", "Pydantic data models")
        svc.build_prompt("proj", "claude", query="FAISS", surface="test")
    finally:
        store.close()
    names = _names(exporter)
    assert {"cb.import_text", "cb.extract", "cb.redact", "cb.retrieve", "cb.build_prompt"} <= set(
        names
    )
    assert "cb.store.save" not in names  # SQLite is not traced at the store level
    for s in exporter.get_finished_spans():
        blob = " ".join(str(v) for v in s.attributes.values())
        assert "Pydantic" not in blob and "Tirth" not in blob
        assert all(k.startswith("cb.") for k in s.attributes)
    imp = next(s for s in exporter.get_finished_spans() if s.name == "cb.import_text")
    assert imp.attributes["cb.package_name"] == "proj" and imp.attributes["cb.added"] > 0
    ret = next(s for s in exporter.get_finished_spans() if s.name == "cb.retrieve")
    assert ret.attributes["cb.method"] == "lexical" and ret.attributes["cb.selected"] >= 1


def test_errors_are_recorded(exporter, tmp_path):
    store = SQLiteStore(base_dir=tmp_path)
    svc = ContextBridgeService(store, settings=Settings(storage_dir=tmp_path))
    try:
        with pytest.raises(ValidationError):
            svc.import_text("   ", "proj")
        with pytest.raises(PackageNotFoundError):
            svc.merge_packages(["a", "b"], "c")
    finally:
        store.close()
    # A validation error before the span opens produces no span; the merge
    # span opens first and records the exception.
    spans = exporter.get_finished_spans()
    merge = next(s for s in spans if s.name == "cb.merge")
    assert merge.status.status_code.name == "ERROR"
    assert any(e.name == "exception" for e in merge.events)


def test_traced_decorator_and_attribute_cleaning(exporter):
    @telemetry.traced("custom", static="yes")
    def fn(x, *, package_name="", name="", target_model=None):
        telemetry.set_attribute("list", ["a", "b"])
        telemetry.set_attribute("obj", object())
        telemetry.set_attribute("none", None)
        return x

    assert fn(1, package_name="p", target_model="claude") == 1
    assert fn(1, name="q") == 1
    spans = [s for s in exporter.get_finished_spans() if s.name == "cb.custom"]
    assert len(spans) == 2
    attrs = spans[0].attributes
    assert attrs["cb.static"] == "yes" and attrs["cb.package_name"] == "p"
    assert attrs["cb.target_model"] == "claude" and list(attrs["cb.list"]) == ["a", "b"]
    assert "cb.none" not in attrs and attrs["cb.obj"].startswith("<object")
    assert spans[1].attributes["cb.package_name"] == "q"


def test_fastapi_instrumentation(exporter, tmp_path):
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    from fastapi.testclient import TestClient

    from contextbridge.api import create_app

    settings = Settings(storage_dir=tmp_path, otel_enabled=True)
    store = SQLiteStore(base_dir=tmp_path)
    app = create_app(settings, store=store, configure_tracing=False)
    assert telemetry.instrument_fastapi(app)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["tracing"] is True
        assert client.get("/livez").status_code == 200
    store.close()
    http_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("GET")]
    assert http_spans, _names(exporter)
    assert not any("livez" in s.name for s in http_spans)


def test_configure_is_idempotent_and_console(exporter, capsys):
    assert telemetry.configure(service_name="again") is True  # already configured → True
    assert telemetry.configure(service_name="console", console=True, force=True) is True
    with telemetry.span("hello", n=1):
        pass
    telemetry.shutdown()
    assert "cb.hello" in capsys.readouterr().out
    assert telemetry.tracer() is None
