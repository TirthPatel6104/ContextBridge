# Changelog

All notable changes to ContextBridge are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] — 2026-09-22

The "production" release: a FastAPI server, a PostgreSQL + pgvector backend
with published benchmarks, a golden retrieval harness that gates CI, Docker
and AWS deployment, and OpenTelemetry tracing.

### Added
- **FastAPI server** (`contextbridge.api`, `cb serve`): the same JSON API the
  dashboard and extension use, versioned under `/api/v1` with OpenAPI docs at
  `/docs`, the unversioned `/api` alias kept for compatibility, `/livez` and
  `/readyz` probes, optional API-key authentication (`CB_API_KEY`, sent as
  `X-API-Key` or `Authorization: Bearer`), a `Content-Length` upload guard,
  uniform `{"error": …}` responses, and the dashboard served from the same
  process. New endpoints: `GET /api/v1/ready`, `GET /api/v1/eval/golden`,
  `GET /api/v1/packages/<name>/embeddings`.
- **PostgreSQL + pgvector backend** (`contextbridge[postgres]`,
  `CB_DATABASE_URL`): packages, versions and the egress ledger in Postgres,
  plus persisted item embeddings with an HNSW index per vector dimension.
  Hybrid retrieval embeds only new or changed items and drops vectors for
  removed ones. Numbered SQL migrations (`storage/sql/`) applied on start or
  with `cb db upgrade`; `cb db status`; `cb migrate --to postgres` copies a
  SQLite installation (all versions + ledger) without deleting anything.
- **Benchmarks** (`benchmarks/bench.py`, `docs/BENCHMARKS.md`, a manual
  GitHub Actions workflow): package round-trips SQLite vs Postgres, FAISS /
  numpy vs pgvector exact vs HNSW at 1k–50k vectors including recall against
  exact search, and the cost of re-embedding per request vs. persisted
  embeddings.
- **Golden retrieval harness** (`cb eval --golden`, `evaluation/golden.py`):
  84 hand-written query → relevant-item pairs over six persona memory sets,
  tagged lexical / paraphrase / semantic / multi, scored with Hit@1, P@k,
  R@k, MRR and nDCG@k overall and per tag. A published baseline
  (`golden_baseline.json`) and `--check-baseline` fail CI on regressions;
  `--update-baseline` regenerates it.
- **OpenTelemetry tracing** (`contextbridge[otel]`, `telemetry.py`): spans
  for import, extract, redact, retrieve, prompt build, merge, Postgres
  operations and HTTP requests; exported over OTLP/HTTP when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Attributes are names and counts only;
  never memory content.
- **Docker**: multi-stage non-root image with a `/readyz` healthcheck;
  `docker-compose.yml` with pgvector Postgres and an optional
  `observability` profile (OpenTelemetry collector + Jaeger).
- **CI/CD**: lint, tests on Python 3.11–3.13 against a pgvector service
  container with an 80% coverage gate, the evaluation harnesses with a job
  summary and uploaded reports, a Docker build plus compose end-to-end smoke
  test (`scripts/e2e.py`), and a `Deploy to AWS` workflow (OIDC → ECR →
  App Runner → live smoke test).
- **AWS infrastructure** (`infra/aws`, Terraform): ECR, RDS PostgreSQL 16
  with pgvector, App Runner with a VPC connector and Secrets Manager
  injection, autoscaling, and a GitHub OIDC deploy role.
- Dashboard: sends `X-API-Key` from `localStorage` and shows an inline field
  when the server answers `401`.
- Docs: `docs/DEPLOYMENT.md`, `docs/OBSERVABILITY.md`, `docs/BENCHMARKS.md`;
  `ARCHITECTURE.md` and `EVALUATION.md` updated.

### Changed
- `Settings` gained `database_url`, `api_key`, `environment`, `otel_*`;
  `CB_STORAGE_BACKEND` accepts `postgres`, and a `CB_DATABASE_URL` alone
  selects it.
- `ContextBridgeService.retrieve_from_memory` takes `package_name` so the
  Postgres backend can persist embeddings per package.
- `MemoryRetriever.index` honours persistent vector stores (no clearing,
  incremental embedding).
- `pyproject`: extras `api`, `postgres`, `otel`, `all`; `dev` pulls all of
  them; coverage configuration with `fail_under = 80`; package data includes
  `storage/sql/*.sql`.

## [0.3.0] — 2026-09-15

The "trust boundaries" release: decide per item who may see it, see where
memory has gone, and keep memory true as decisions change.

### Added
- **Sharing boundaries per item** (`MemoryItem.sharing`: `any`, `local_only`,
  `never`). Prompt builds partition the memory by the target's kind (cloud vs.
  local; unknown names count as cloud), render only what is allowed, and list
  every withheld item with its reason (`cb share`, `cb prompt` output,
  `POST /api/packages/<name>/items/sharing`, dashboard step 2 and step 4,
  extension status line).
- **Egress ledger.** Every prompt build and `cb query` records the target,
  its kind, the surface (cli / dashboard / extension / query), the rendered
  item ids, withheld count, and token estimate — never the text. Stored in a
  new `egress_log` table (SQLite) or `egress.jsonl` per package (JSON).
  `cb audit [--json] [--clear]`, `GET|DELETE /api/packages/<name>/audit`,
  and the dashboard "Ledger & health" section show per-event and per-item
  views, including which items have ever reached a cloud model.
  `--dry-run` / `record: false` previews without a ledger entry.
- **Item lifecycle** (`MemoryItem.status`: `active`, `superseded`, `done`;
  `superseded_by`). Superseded and done items stay in the package and history
  but leave prompts and retrieval (`RetrievalOptions.include_inactive` to
  override). `cb done`, `cb reopen`, `cb supersede`,
  `POST /api/packages/<name>/items/status|supersede`.
- **Import-time contradiction detection.** Appending to a package compares the
  new items against the *active* stored ones (cross-boundary only) and
  records possible contradictions as pending conflicts in package metadata.
  `cb conflicts [--resolve OLD NEW keep_new|keep_old|dismiss]`,
  `GET /api/packages/<name>/conflicts`,
  `POST /api/packages/<name>/conflicts/resolve`, and a review panel in the
  dashboard. Resolution supersedes the loser; nothing is deleted.
- **Corroboration tracking** (`first_seen`, `last_seen`, `seen_count`).
  Re-importing a statement updates the existing item (sightings, origins,
  empty source excerpt) instead of ignoring it. Status and sharing survive a
  re-sighting.
- **Hand-off detection.** Transcripts that start with a ContextBridge prompt
  are recognised by the provenance footer; the pasted block is stripped
  before extraction and the link (`metadata.handoffs`) is recorded.
  Importing a file that contains *only* a pasted prompt is rejected.
- **Health report** (`cb health [--stale-days N]`,
  `GET /api/packages/<name>/health`, dashboard): status counts, corroborated
  vs. single-sighting items, stale items, open tasks, pending conflicts,
  items shared with a cloud model, items never shared, hand-offs.
- Version history entries record `modified` (tracked-field changes) and
  rollback restores them; `cb history` and the dashboard show a "Changed"
  column.
- `MergeResult`-style `PackageMerger.cross_conflicts()` for stored-vs-incoming
  comparison.

### Changed
- `SCHEMA_VERSION` is 3. 0.2.x packages load unchanged: new item fields
  default to `any` / `active` / 1 sighting, with `first_seen` / `last_seen`
  set on first load.
- `cb inspect` and the dashboard mark superseded / done items, sharing
  restrictions, and `seen ×N`.
- `/api/retrieve` item payloads and `/api/packages/<name>` include the new
  fields plus `status_counts`, `sharing_counts`, `active_items`,
  `pending_conflicts`, and `handoffs`.
- `/api/extract` returns `re_seen_items`, `notes`, `handoff`, and
  `conflicts`.
- `cb query` builds its context through the service so sharing policies and
  the ledger apply there too.
- SQLite `user_version` is 2 (additive table; no data migration needed).

## [0.2.0] — 2026-09-14

The "portable working memory" release: durable storage, explainable retrieval,
privacy redaction, reviewable merges, and an offline evaluation suite.

### Added
- **SQLite storage backend** (`SQLiteStore`), now the default. Legacy JSON
  packages are imported automatically (and left in place); `cb migrate` runs
  the import explicitly; `cb info` shows backend and location.
- **Portable package files**: `cb export-package` / `cb import-package` and
  `GET /api/packages/<name>/export` / `POST /api/packages/import`.
- **Explainable retrieval**: BM25 lexical scoring with matched terms and a
  human-readable reason per item, optional semantic-embedding blending,
  category filters, top-k, minimum score, token budgets, and estimated token
  savings vs. the full memory and the source transcript (`cb retrieve`,
  `POST /api/retrieve`, dashboard step 3).
- **Secrets/PII redaction** before storage and export, on by default, with a
  transparent report of what was masked (`cb scan`, `--no-redact`,
  `POST /api/redaction/scan`).
- **Package merging** with near-duplicate folding, preserved origins, and
  flagged possible conflicts; dry-run preview (`cb merge`,
  `POST /api/packages/merge`).
- **Review workflow**: item ids, provenance (`origin`), confidence, source
  excerpts and redaction flags on every item; remove items as a new version
  (`cb remove`, `POST /api/packages/<name>/items/remove`); rollback from the
  dashboard.
- **Offline evaluation suite** (`cb eval`, `GET /api/eval`, dashboard panel)
  measuring extraction coverage, retrieval P@k / R@k / MRR, duplicate F1,
  redaction recall and false positives, and token savings over bundled
  fixtures. No API calls.
- **Service layer** (`ContextBridgeService`) shared by CLI and web app;
  `create_app()` factory with injectable store; typed `Settings` from `CB_*`
  environment variables.
- Docs: `docs/ARCHITECTURE.md`, `docs/PRIVACY.md`, `docs/EVALUATION.md`.

### Changed
- `MemoryItem` gained `id`, `origin`, and `redactions`; `ContextPackage`
  gained `schema_version`. All new fields have defaults, so 0.1.x packages load
  unchanged.
- Version history entries now carry the version they *produced* (previously two
  entries were both labelled v1 after the first update).
- The `local` extraction engine now means the **offline rule-based extractor**
  everywhere (the dashboard already used it that way). Use `ollama` or an
  Ollama model name (e.g. `--engine llama3`) for local LLM extraction.
- `/api/prompt` no longer injects attached files or the watch folder unless
  `include_files` / `include_watch` is set. The extension passes
  `include_files: true` for files the user attached in its panel.
- Dashboard rebuilt as a four-step workflow (import → review → retrieve →
  export) with semantic HTML, keyboard-accessible controls, light/dark themes,
  and no external font or script loads.
- The web server binds to `127.0.0.1` and restricts CORS to the chat sites the
  extension runs on.
- Package names, upload file names, upload sizes, and import documents are
  validated. Unexpected errors no longer echo tracebacks to the client.
- Vector store infers its dimension from the first batch (fixes a crash when
  using 1536-dimensional OpenAI embeddings from the CLI).
- Claude's hash-based pseudo-embeddings are flagged as non-semantic and no
  longer influence ranking.

### Removed
- Debug logging of raw LLM responses (they contain the user's memory).

## [0.1.0] — initial public release

- OpenAI, Claude, and Ollama adapters; rule-based local extractor.
- Versioned JSON context packages with diffs and rollback.
- FAISS-backed retrieval, per-model prompt formatting.
- Click CLI, Flask dashboard, Chrome extension.
