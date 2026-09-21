# Observability

ContextBridge exports **OpenTelemetry traces** for the request path and for the
service operations underneath it. Tracing is optional (an extra), off by
default, and — like the logs — never carries memory content.

## Enabling it

```bash
pip install "contextbridge[otel]"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318     # OTLP/HTTP collector
export OTEL_SERVICE_NAME=contextbridge                        # optional
cb serve
```

Setting an endpoint enables tracing; `CB_OTEL_ENABLED=true` enables it without an exporter (spans are recorded locally, useful with `CB_OTEL_CONSOLE=true`, which prints every span to stdout). `CB_ENVIRONMENT` becomes the `deployment.environment` resource attribute.

Locally, `docker compose --profile observability up` runs a collector and Jaeger; open http://localhost:16686, pick the `contextbridge` service and search.

## What a trace looks like

```
GET /api/v1/prompt                      (FastAPI instrumentation: route, status, duration)
└── cb.build_prompt                     package_name, target_model, surface, record, narrowed
    ├── cb.store.load                   backend=postgres, package_name           (Postgres only)
    ├── cb.retrieve                     package_name, items, top_k, semantic → selected, method, tokens_selected
    │   ├── cb.retriever.embed          total, new, stale                       (persistent vector store)
    │   ├── cb.store.upsert_embeddings  count, dimension
    │   └── cb.store.search_embeddings  top_k, exact, dimension
    └── (attributes on cb.build_prompt) included, withheld, tokens_prompt
```

Spans emitted by the service layer:

| Span | Attributes (all prefixed `cb.`) |
|---|---|
| `cb.import_text` | `package_name`, `engine`, `mode`, `chars` → `extracted`, `redacted_values`, `added`, `re_seen`, `conflicts`, `created` |
| `cb.extract`, `cb.redact` | `engine` / `enabled` and the resulting counts |
| `cb.retrieve` | `package_name`, `items`, `top_k`, `semantic` → `selected`, `method`, `tokens_selected` |
| `cb.build_prompt` | `package_name`, `target_model`, `surface`, `record`, `narrowed` → `included`, `withheld`, `tokens_prompt` |
| `cb.merge` | `package_name`, `sources`, `dry_run` → `duplicates`, `conflicts` |
| `cb.store.*` | Postgres operations with `backend`, `package_name`, counts, `dimension` |

Exceptions are recorded on the span (`exception` event, status `ERROR`) and re-raised.

## What is deliberately absent

* transcript text, item content, rendered prompts, queries (only their *length* or count), API keys, DSN passwords;
* HTTP spans for `/livez`, `/readyz` and `/static/*` (excluded to keep probes out of the trace volume).

The test suite asserts this: `tests/test_telemetry.py` runs a full import → retrieve → prompt flow through an in-memory exporter and checks that no attribute contains memory content.

## Using it in code

```python
from contextbridge.telemetry import span, set_attribute, traced

with span("my_step", package_name=name, top_k=5):
    ...
    set_attribute("selected", 3)

@traced("my_function")           # copies package_name / target_model / engine / surface kwargs
def my_function(*, package_name: str): ...
```

Both are no-ops when tracing is not configured or the SDK is not installed, so library code can use them freely.

## Metrics and logs

There is no metrics exporter yet; the HTTP instrumentation gives request duration and status per route, and spans carry the counts you would otherwise put in gauges (items, tokens, withheld). Logs are plain Python logging (`CB_LOG_LEVEL`); in containers they go to stdout for the platform to collect.
