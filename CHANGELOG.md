# Changelog

All notable changes to ContextBridge are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
