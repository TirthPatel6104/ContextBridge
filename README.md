<p align="center">
  <h1 align="center">🌉 ContextBridge</h1>
  <p align="center"><strong>Portable, privacy-conscious working memory for people who move between ChatGPT, Claude, and local models.</strong></p>
  <p align="center">Extract structured memory from a conversation, review and redact it, retrieve only what the next task needs, and paste it into any model — with every selection explained, every item's sharing boundary enforced, and every hand-off recorded.</p>
</p>

<p align="center">
  <a href="https://github.com/TirthPatel6104/ContextBridge/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TirthPatel6104/ContextBridge/actions/workflows/ci.yml/badge.svg" /></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white" />
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-92%25%20(gate%2080%25)-brightgreen" />
  <img alt="Tests" src="https://img.shields.io/badge/tests-297-brightgreen" />
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green" />
  <img alt="Version" src="https://img.shields.io/badge/version-0.4.0-purple" />
</p>

---

## The problem

You spend an afternoon in ChatGPT designing a database schema. The next morning you open Claude to write the migration, and it knows nothing: not your name, not the stack, not the three decisions you already argued through, not the two tasks still open. So you re-explain — badly, from memory, burning tokens on a wall of pasted chat that is mostly noise.

Every AI tool keeps its own memory, none of them share it, and none of them let you see, edit, or audit what they remember. Pasting whole transcripts is the workaround, and it leaks secrets, wastes context windows, and still loses the thread.

## What ContextBridge does

ContextBridge turns a conversation into **reviewable, portable, structured memory** that you own:

| Step | What happens | Why it matters |
|---|---|---|
| **1. Import** | A transcript, ChatGPT export ZIP, saved page, PDF, or Markdown is parsed locally and decomposed into six categories: identity, projects, facts, decisions, open tasks, preferences. Works fully offline with a rule-based extractor, or with an LLM when you want quality over privacy. | Memory becomes data with a schema, not a blob. |
| **2. Review & redact** | Every item shows its confidence, the source excerpt it came from, and where it came from. Secrets and personal data are masked before storage. You remove what is wrong; removals are versioned and reversible. | You see and control exactly what will be carried forward. |
| **3. Retrieve** | Type what you are about to do. Items are scored, and the result shows *which* were selected, their scores, the matched terms, and the estimated tokens saved. Lexical BM25 by default; hybrid with embeddings when an adapter provides them, persisted in pgvector on the server. | No black-box retrieval; no wasted context window. |
| **4. Export** | The selection is rendered the way the target model prefers (XML for Claude, Markdown for ChatGPT, plain text for Ollama) with a provenance footer. Items you marked *local only* are withheld from cloud targets and listed, and the build is written to the **egress ledger**. | Continuity across tools without vendor lock-in, and no silent leaks. |
| **✓ Ledger & health** | See which item ids went to which model and when, which items never left the machine, which are corroborated by several conversations, which have gone stale, and which new statements contradict what you already hold. | Memory you can audit and keep true, not just accumulate. |

Everything runs on `127.0.0.1` by default. The only time a transcript leaves your machine is when you explicitly choose a vendor extraction engine. When you want the same memory shared across machines or teammates, the same code runs as a **FastAPI service on PostgreSQL + pgvector**, in Docker, on AWS, with an API key and OpenTelemetry traces — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### What is different from ChatGPT Memory, Claude memory, or a memory SDK

| | Vendor memory (ChatGPT, Claude, Gemini) | Memory SDKs (Mem0, Zep, Letta) | ContextBridge |
|---|---|---|---|
| Works across vendors | Import via a pasted prompt; no structured export | Yes, for the app you build | Yes, for you, with no code |
| Runs without an API key | n/a | Usually hosted | Yes, offline by default |
| Per-item **sharing boundary** (this fact may go to a local model but never to a cloud one) | No | No | **Yes** — `any` / `local_only` / `never`, enforced on every prompt build |
| **Egress ledger** (which memory went to which model, when) | No | No | **Yes** — item ids, target, surface, tokens; never the text |
| Contradictions **flagged, not overwritten** | Overwrites silently | Varies | **Yes** — old item kept as *superseded* with a pointer to its replacement |
| Retrieval you can inspect (scores, matched terms, withheld items) and **measure** (84-pair golden harness in CI) | No | Partly | Yes |
| Self-hostable service with probes, auth, tracing, migrations | n/a | Some | Yes — FastAPI + Postgres/pgvector, Docker, Terraform for AWS |

The honest trade-off: vendor memory is zero-effort inside its own walls, and ContextBridge's default extractor is rule-based and its offline retrieval is lexical. The numbers are in [docs/EVALUATION.md](docs/EVALUATION.md).

## Quick start

```bash
git clone https://github.com/TirthPatel6104/ContextBridge.git
cd ContextBridge
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                           # or ".[api]" for the server only
```

No API keys are needed for the default workflow.

```bash
# 1. Import a conversation (offline rule-based extraction, redaction on)
cb import experiments/sample_chat.txt -o contextbridge_design

# 2. Review what was extracted (ids let you remove items)
cb inspect contextbridge_design --ids

# 3. See exactly which items a task would pull in, and why
cb retrieve contextbridge_design "vector search decisions" --top-k 3

# 4. Get a paste-ready prompt for the next model, scoped to that task
cb prompt contextbridge_design --model claude --query "vector search decisions" --copy

# 5. Keep one item away from cloud models, then see what each model has received
cb share contextbridge_design <ITEM_ID> --policy local_only
cb prompt contextbridge_design --model claude       # that item is withheld and listed
cb audit contextbridge_design                        # the egress ledger
cb health contextbridge_design                       # stale items, open tasks, conflicts
```

Then start the server for the same workflow with a UI and a JSON API:

```bash
cb serve                     # → http://127.0.0.1:5000  (dashboard)  ·  /docs (OpenAPI)
```

### Run it as a service

```bash
docker compose up --build                          # app + pgvector Postgres on :8000
docker compose --profile observability up --build  # + OpenTelemetry collector + Jaeger on :16686
python scripts/e2e.py --base-url http://localhost:8000   # the same smoke test CI runs
```

Or point the CLI / server at any pgvector-enabled Postgres and copy your local memory across:

```bash
export CB_DATABASE_URL=postgresql://user:pass@host:5432/contextbridge
cb db upgrade                    # apply schema migrations
cb migrate --to postgres         # copy every package, version, and ledger entry from SQLite
CB_API_KEY=$(openssl rand -hex 24) cb serve --host 0.0.0.0 --port 8000
```

AWS: `infra/aws/` provisions ECR + RDS (pgvector) + App Runner + an OIDC deploy role with Terraform, and `.github/workflows/deploy.yml` pushes every `main` commit to it and smoke-tests the live URL. Step by step in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Four surfaces, one engine

| Surface | Use it for |
|---|---|
| **`cb` CLI** | Scripted or terminal workflows: import, retrieve, prompt, share, done, supersede, conflicts, audit, health, merge, export/import package files, evaluation, serve, migrate, db |
| **FastAPI server** (`cb serve`) | The JSON API (`/api/v1`, OpenAPI at `/docs`), the dashboard at `/`, `/livez` + `/readyz` probes, optional `X-API-Key` auth, OpenTelemetry spans |
| **Dashboard** | The guided import → review → retrieve → export workflow, the ledger & health panel, package tools, and the evaluation panel |
| **Chrome extension** | A floating button on chatgpt.com / claude.ai that sends the open conversation to your server and hands back a prompt for the other model |

All of them call the same `ContextBridgeService`, so behaviour is identical. The original Flask dashboard (`python -m contextbridge.web.app`) is still shipped for compatibility.

## Architecture

```mermaid
graph LR
    subgraph Surfaces
        CLI["cb CLI"]
        API["FastAPI<br/>/api/v1 · /docs · probes"]
        WEB["Dashboard"]
        EXT["Chrome extension"]
    end
    SVC["ContextBridgeService"]
    subgraph Core
        EXTR["Extractors<br/>(rules / LLM)"]
        RED["Redaction"]
        LIFE["Lifecycle<br/>(hand-offs, supersession, health)"]
        PKG["Packager<br/>(versions, diffs)"]
        RET["Retriever<br/>(BM25 + optional embeddings)"]
        MRG["Merger<br/>(duplicates, conflicts)"]
        SHR["Sharing boundaries"]
        PB["Prompt builder"]
    end
    subgraph Storage
        SQL["SQLite (default, local)"]
        PG["PostgreSQL + pgvector<br/>(service deployments)"]
        PORT["Portable export"]
        LEDGER["Egress ledger"]
    end
    OTEL["OpenTelemetry<br/>(spans, no content)"]
    EXT -->|allowed origins + API key| API
    WEB --> API
    CLI --> SVC
    API --> SVC
    SVC --> EXTR --> RED --> LIFE --> PKG --> SQL
    PKG --> PG
    SVC --> SHR --> RET
    RET -.->|persisted embeddings| PG
    SHR --> PB --> LEDGER
    SVC --> MRG
    PKG --> PORT
    SVC -.-> OTEL
    API -.-> OTEL
```

Deployment topology on AWS:

```mermaid
graph LR
    GH["GitHub Actions<br/>(OIDC, no static keys)"] -->|build + push| ECR["Amazon ECR"]
    GH -->|update-service| AR
    subgraph AWS
        AR["App Runner<br/>1–3 instances · /readyz"] -->|VPC connector| RDS["RDS PostgreSQL 16<br/>+ pgvector"]
        SM["Secrets Manager<br/>DSN · API key"] --> AR
        AR -.->|OTLP| COL["OTel collector<br/>(optional)"]
    end
    U["Browser / extension / CLI"] -->|HTTPS + X-API-Key| AR
```

Design notes, the data model, and the storage schema are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Data model in one glance

```json
{
  "name": "contextbridge_design",
  "version": 3,
  "schema_version": 3,
  "memory": {
    "decisions": [
      {
        "id": "9c1f3e2a7b4d",
        "category": "decisions",
        "content": "Using Pydantic for data models and FAISS for vector search",
        "confidence": 0.7,
        "source": "rule:decision_keyword",
        "origin": "sample_chat.txt | claude_followup.txt",
        "redactions": [],
        "sharing": "any",
        "status": "superseded",
        "superseded_by": "4d1e0a9c77b2",
        "first_seen": "2026-09-10T09:12:00Z",
        "last_seen": "2026-09-14T18:40:00Z",
        "seen_count": 2
      }
    ]
  },
  "history": [
    {"version": 1, "note": "created", "diff": {"added": ["…"], "removed": [], "modified": []}},
    {"version": 2, "note": "imported", "diff": {"added": ["…"], "removed": [], "modified": []}},
    {"version": 3, "note": "9c1f3e2a7b4d superseded by 4d1e0a9c77b2", "diff": {"added": [], "removed": [], "modified": [{"id": "9c1f3e2a7b4d", "before": {"status": "active"}, "after": {"status": "superseded"}}]}}
  ],
  "metadata": {
    "handoffs": [{"package_name": "contextbridge_design", "package_version": 1, "origin": "claude_followup.txt"}]
  }
}
```

Item ids are derived from category + normalised content, so the same statement gets the same id across re-extractions, diffs, merges, and embedding caches. The egress ledger lives next to the package (a table in SQLite and Postgres, a `.jsonl` file per package in the JSON backend) and stores item ids, never text.

## Privacy model

* **Local-first by default.** Storage is a SQLite file under `~/.contextbridge`. The dashboard loads no external fonts or scripts and binds to loopback. CORS is limited to the chat sites the extension runs on.
* **Redaction before storage.** API keys, tokens, JWTs, private keys, credential assignments, card numbers, SSNs, emails, phone numbers, and IP addresses are replaced with `[REDACTED:KIND]` placeholders. The item records only the kind and count.
* **Nothing is logged or traced that you would not want in a log.** Counts and package names, yes; transcript text, memory content, prompts, queries, keys, and DSN passwords, no. The test suite asserts this for traces.
* **Explicit egress only.** Choosing `openai` or `claude` as an extraction engine sends the transcript to that vendor. `cb query` sends the question and selected memory to the model you name. Nothing else leaves the machine.
* **Sharing boundaries per item.** Mark an item `local_only` and it is rendered for Ollama but withheld from ChatGPT and Claude; mark it `never` and it stays in your memory without ever entering a prompt. An unknown target name is treated as cloud.
* **An egress ledger you can read.** Every prompt build and `cb query` records the target, its kind, which surface asked (CLI, dashboard, extension, API), the item ids, and token count.
* **As a service.** `CB_API_KEY` guards the API (constant-time compare, never logged); the database is private; secrets come from Secrets Manager; deployments use OIDC.

Full details: [docs/PRIVACY.md](docs/PRIVACY.md).

## How it evaluates itself

Two offline harnesses run in CI on every push (no API calls, deterministic, seconds):

**Golden retrieval harness** (`cb eval --golden`): 84 hand-written query → relevant-item pairs over six persona memory sets, tagged by difficulty. CI fails when a guarded metric drops more than 0.02 below the published baseline.

| Slice (v0.4.0, lexical BM25, k = 3) | Pairs | Hit@1 | R@3 | MRR | nDCG@3 |
|---|---|---|---|---|---|
| **all** | 84 | 77.4% | 84.1% | 82.1% | 81.5% |
| lexical (shared vocabulary) | 46 | 100.0% | 98.9% | 100.0% | 99.2% |
| paraphrase (reworded) | 28 | 57.1% | 79.2% | 71.4% | 71.0% |
| multi (several answers) | 7 | 71.4% | 66.7% | 85.7% | 67.1% |
| semantic (no shared words) | 10 | 30.0% | 30.0% | 30.0% | 30.0% |

The `semantic` slice is expected to fail under lexical retrieval; it exists to measure the headroom embeddings buy (`run_golden(adapter=…)` scores the hybrid path).

**Component suite** (`cb eval`): extraction coverage 100% (on fixtures written the way the rules expect), retrieval MRR 86.7%, duplicate F1 75.0% (conservative by design), redaction recall 100% with 0 false positives, token savings 93% vs. transcript. What each number does and does not mean is in [docs/EVALUATION.md](docs/EVALUATION.md).

## Benchmarks

`benchmarks/bench.py` measures SQLite vs Postgres on the package path and FAISS / numpy vs pgvector (exact and HNSW, with recall against exact search) at 1k–50k vectors, plus what persisted embeddings save on a hybrid query. The CI *Benchmarks* job runs it on a GitHub `ubuntu-latest` runner so the published numbers name their hardware; the tables and their interpretation are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

<!-- BENCH:START -->
_The summary table is refreshed from `benchmarks/results/github-runner.md` after each release's CI run._
<!-- BENCH:END -->

## Observability

With `pip install "contextbridge[otel]"` and `OTEL_EXPORTER_OTLP_ENDPOINT` set, every request produces a trace: the HTTP span, then `cb.build_prompt` → `cb.retrieve` → `cb.store.search_embeddings` with package names, item counts, tokens, target and engine names, timings, and recorded exceptions — never content. `docker compose --profile observability up` gives you a collector and Jaeger locally. [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

## Configuration

Copy `.env.example` to `.env`. Everything is optional.

| Variable | Default | Purpose |
|---|---|---|
| `CB_STORAGE_DIR` | `~/.contextbridge` | Where SQLite packages and attached files live |
| `CB_STORAGE_BACKEND` | `sqlite` | `sqlite`, `postgres`, or `json` (legacy) |
| `CB_DATABASE_URL` | — | PostgreSQL DSN; selects the postgres backend when set |
| `CB_API_KEY` | — | Shared secret for the HTTP API (`X-API-Key`); empty = open, loopback only |
| `CB_REDACT` | `true` | Redact secrets/PII before storing |
| `CB_DEFAULT_ADAPTER` | `local` | Default extraction engine |
| `CB_HOST` / `CB_PORT` | `127.0.0.1` / `5000` | Server bind address (container: `0.0.0.0:8000`) |
| `CB_ALLOWED_ORIGINS` | chatgpt.com, chat.openai.com, claude.ai | Browser origins allowed to call the API |
| `CB_ENVIRONMENT` | `development` | Reported in health and traces |
| `CB_MAX_UPLOAD_MB` | `25` | Upload size limit |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Enables trace export |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | — | Only for the matching engine / query target |

Extraction engines: `local` (offline rules), `openai`, `claude`, or any Ollama model name (e.g. `--engine llama3`).

## CLI reference

| Command | What it does |
|---|---|
| `cb import FILES… -o NAME [--engine E] [--no-redact] [--mode append\|replace]` | Extract memory from transcripts / exports / PDFs / Markdown |
| `cb retrieve NAME "query" [-k N] [-c category]… [--budget T] [--min-score S] [--json]` | Explainable retrieval with token savings |
| `cb prompt NAME --model claude\|openai\|local [-q "query"] [--copy] [--raw] [--dry-run]` | Paste-ready prompt, optionally scoped by retrieval; recorded in the ledger unless `--dry-run` |
| `cb query "question" -c NAME -m MODEL` | Ask a model with relevant memory injected (calls the API; recorded) |
| `cb list` / `cb inspect NAME [--ids]` / `cb history NAME` | Browse packages, items, and versions |
| `cb remove NAME ID…` / `cb rollback NAME -v N` / `cb delete NAME` | Curate and undo |
| `cb share NAME ID… --policy any\|local_only\|never` | Who may see an item when a prompt is built |
| `cb done NAME ID…` / `cb reopen NAME ID…` / `cb supersede NAME OLD NEW` | Lifecycle |
| `cb conflicts NAME [--resolve OLD NEW keep_new\|keep_old\|dismiss]` | Settle statements that may contradict stored memory |
| `cb audit NAME [--json] [--clear]` / `cb health NAME [--stale-days N]` | Egress ledger; stale items, open tasks, exposure |
| `cb export-package NAME [-o FILE]` / `cb import-package FILE [--name N] [--overwrite]` | Portable JSON files |
| `cb merge A B… -o NEW [--dry-run]` | Merge with duplicate folding and conflict flags |
| `cb scan FILES…` | Preview what redaction would mask |
| `cb eval [--json] [-o FILE]` / `cb eval --golden [--check-baseline] [--update-baseline FILE]` | Component suite / golden retrieval harness with regression gate |
| `cb serve [--host H] [--port P] [--reload]` | FastAPI server: dashboard, API, `/docs`, probes |
| `cb migrate` / `cb migrate --to postgres [--database-url DSN] [--only NAME]… [--overwrite]` | Legacy JSON → SQLite; SQLite → PostgreSQL (nothing deleted) |
| `cb db status` / `cb db upgrade` | PostgreSQL schema version and migrations |
| `cb info` | Storage backend, location, stats |

## HTTP API

`GET /docs` is the interactive reference. The routes, all under `/api/v1` (and mirrored under `/api` for the dashboard and extension):

| Route | Purpose |
|---|---|
| `GET /health`, `GET /ready` · `GET /livez`, `GET /readyz` (root) | Configuration and probes |
| `POST /extract` (multipart) · `POST /redaction/scan` | Import transcripts into a package; preview redaction |
| `POST /retrieve` · `POST /prompt` | Explainable retrieval; paste-ready prompt with withheld items and ledger record |
| `GET /packages` · `GET|DELETE /packages/{name}` · `/history` · `POST /rollback` | Packages and versions |
| `POST /packages/{name}/items/remove|sharing|status|supersede` | Review, boundaries, lifecycle |
| `GET /packages/{name}/conflicts` · `POST …/conflicts/resolve` | Contradictions |
| `GET|DELETE /packages/{name}/audit` · `GET /packages/{name}/health` · `GET …/embeddings` | Ledger, health, persisted vectors |
| `GET /packages/{name}/export` · `POST /packages/import` · `POST /packages/merge` | Portability and merging |
| `POST /files/attach` · `GET /files/{name}` · `GET /watch/files` | Opt-in extras for prompts |
| `GET /eval` · `GET /eval/golden` · `GET /local/models` | Evaluation harnesses; Ollama models |

Errors are always `{"error": "…"}` with `400` (validation), `401` (missing key), `404` (unknown package), `413` (too large), `500` (generic, details in the server log only).

## Chrome extension

1. Start the server (the extension talks to `localhost:5000`).
2. `chrome://extensions` → *Developer mode* → *Load unpacked* → select `extension/`.
3. On chatgpt.com or claude.ai, click the floating button, name the package, and click **Extract this chat**. **Copy prompt** gives you the prompt for the other model, tells you how many items were withheld by their sharing policy, and records the build in the ledger as surface `extension`.

## Development

```bash
pytest -q --cov=contextbridge --cov-fail-under=80    # 297 tests; Postgres tests run when CB_TEST_DATABASE_URL is set
ruff check contextbridge tests benchmarks scripts
ruff format --check contextbridge tests benchmarks scripts
cb eval && cb eval --golden --check-baseline         # evaluation harnesses
docker compose up --build && python scripts/e2e.py --base-url http://localhost:8000
```

CI (`.github/workflows/ci.yml`) runs lint, the suite on Python 3.11 / 3.12 / 3.13 against a `pgvector/pgvector:pg16` service container with the 80% coverage gate, both evaluation harnesses with a job summary and uploaded reports, the benchmarks, and a Docker build plus compose end-to-end smoke test. `deploy.yml` ships `main` to AWS; `benchmarks.yml` re-runs the benchmarks with custom sizes.

Project layout:

```
contextbridge/
├── models.py            # Pydantic schema: MemoryItem, ContextPackage, EgressRecord, retrieval models
├── config.py            # Settings from CB_* / OTEL_* env vars
├── service.py           # ContextBridgeService: the one place the pieces are wired
├── telemetry.py         # OpenTelemetry spans (no-op without the SDK)
├── cli.py               # Click CLI (incl. serve, migrate --to postgres, db, eval --golden)
├── api/                 # FastAPI app factory, dependencies (API key), schemas, routers
├── core/                # extractors, redaction, lifecycle, sharing, packager, retriever, merger, prompt_builder
├── adapters/            # OpenAI, Claude, Ollama (LLMInterface)
├── storage/             # SQLiteStore, PostgresStore (+ PgVectorStore, migrations, sql/), JSONStore, portable, VectorStore
├── evaluation/          # component suite runner, golden harness, fixtures + baseline
└── web/                 # dashboard templates/static (served by FastAPI) + legacy Flask app
benchmarks/              # bench.py + published results
scripts/e2e.py           # end-to-end smoke test used by CI and the deploy workflow
infra/aws/               # Terraform: ECR, RDS pgvector, App Runner, OIDC deploy role
deploy/                  # OpenTelemetry collector config for docker compose
extension/               # Chrome MV3 content script
tests/                   # 297 tests (API, Postgres, telemetry, golden harness, CLI, core)
docs/                    # ARCHITECTURE, DEPLOYMENT, OBSERVABILITY, BENCHMARKS, EVALUATION, PRIVACY
```

## Upgrading

**From 0.3** — nothing to do for local use. `pip install -e ".[dev]"` now installs the API, Postgres and OpenTelemetry extras; the Flask dashboard still works, `cb serve` is the new default server. Setting `CB_DATABASE_URL` switches storage to Postgres; `cb migrate --to postgres` copies your SQLite memory across and leaves it in place.

**From 0.2 / 0.1** — see [CHANGELOG.md](CHANGELOG.md); packages written by any earlier version load unchanged.

## Roadmap

Implemented in 0.4.0: FastAPI server with auth, probes and OpenAPI · PostgreSQL + pgvector backend with persisted embeddings and migrations · golden retrieval harness gating CI · benchmarks · Docker and compose · CI with coverage gate and end-to-end smoke test · AWS deployment (Terraform + OIDC workflow) · OpenTelemetry tracing.

Planned:

- [ ] MCP server over the package so Claude Desktop, Claude Code and Cursor can read and write memory live, with sharing boundaries enforced at retrieval time and every read in the ledger
- [ ] Mid-thread hand-off packs: turn the current chat into a structured continuation for another model when the context window fills
- [ ] Export/import in an emerging vendor-neutral memory bundle format with provenance and tombstones
- [ ] Extension: auto-inject at chat start, boundary-filtered, with a "N items sent" badge
- [ ] Recorded-fixture evaluation of LLM extractors with judge-agreement statistics
- [ ] Local semantic embeddings (small sentence-transformer) so the offline path is not purely lexical
- [ ] Team packages with per-member boundaries and a propose/approve queue

## License

MIT — see [LICENSE](LICENSE).
