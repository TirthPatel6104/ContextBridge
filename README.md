<p align="center">
  <h1 align="center">🌉 ContextBridge</h1>
  <p align="center"><strong>Portable, privacy-conscious working memory for people who move between ChatGPT, Claude, and local models.</strong></p>
</p>

<p align="center">
  <a href="https://github.com/TirthPatel6104/ContextBridge/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TirthPatel6104/ContextBridge/actions/workflows/ci.yml/badge.svg" /></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue?logo=python&logoColor=white" />
  <img alt="Coverage" src="https://img.shields.io/badge/coverage-92%25-brightgreen" />
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green" />
  <img alt="Version" src="https://img.shields.io/badge/version-0.4.0-purple" />
</p>

---

You spend an afternoon in ChatGPT designing a database schema. The next morning you open Claude to write the migration and it knows nothing: not your stack, not the three decisions you already argued through, not the two tasks still open. So you paste a wall of chat that leaks secrets, burns tokens, and still loses the thread.

ContextBridge fixes this. It turns a conversation into **structured memory you own**: six categories (identity, projects, facts, decisions, open tasks, preferences), each item with a confidence score, its source excerpt, and a sharing policy. You review it, redact it, and when you move to the next model you say what you are about to do. ContextBridge picks the relevant items, shows you why, renders them the way that model prefers, withholds anything you marked as local-only, and records the hand-off in a ledger. Everything runs offline on your machine by default, with no API key.

## How it works

```mermaid
graph LR
    subgraph Input
        T["Transcript · ChatGPT export ZIP<br/>PDF · Markdown · saved page"]
    end
    subgraph Pipeline
        EX["Extract<br/>(offline rules, or GPT / Claude / Ollama)"]
        RD["Redact<br/>secrets &amp; PII → [REDACTED:KIND]"]
        PK["Package<br/>versioned, diffed, stable item ids"]
        SH["Sharing boundaries<br/>any · local_only · never"]
        RT["Retrieve<br/>BM25, optional embeddings"]
        PB["Render for target<br/>XML · Markdown · plain"]
    end
    subgraph Output
        P["Paste-ready prompt"]
        L["Egress ledger<br/>(item ids, target, tokens)"]
    end
    ST[("SQLite (local)<br/>or Postgres + pgvector")]
    T --> EX --> RD --> PK --> ST
    ST --> SH --> RT --> PB --> P
    PB --> L
```

**Import.** The file is parsed locally and decomposed into memory items. The default extractor is rule-based and needs no network; pass `--engine openai`, `--engine claude`, or an Ollama model name when you want an LLM to do it. Secrets, keys, emails, card numbers and the like are masked before anything touches disk. Each item gets an id derived from its category and normalised content, so the same statement keeps the same id across re-imports, diffs and merges. Re-importing a fact bumps its sighting count instead of duplicating it; a statement that contradicts stored memory is flagged for you to resolve, never silently overwritten.

**Review.** Every item shows its confidence, where it came from, and what was redacted. Removing items, marking tasks done, or recording that one decision superseded another all create a new package version, and any version can be rolled back.

**Retrieve and export.** You describe the next task. Items are scored, and the result lists which were chosen, their scores, the terms that matched, and the tokens saved versus pasting everything. The selection is rendered for the target (XML for Claude, Markdown for ChatGPT, plain text for a local model). Items marked `local_only` are rendered for Ollama but withheld from cloud models; `never` keeps an item in memory without it ever entering a prompt. Each build writes the target, the surface that asked, and the item ids to the egress ledger, so you can always answer "what has Claude seen?"

Four surfaces share one engine: the `cb` CLI, a FastAPI server with a dashboard and OpenAPI docs, a Chrome extension for chatgpt.com and claude.ai, and the Python service class underneath all three. Design notes and the data model are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

Requires Python 3.11 or newer. Not yet on PyPI.

```bash
git clone https://github.com/TirthPatel6104/ContextBridge.git
cd ContextBridge
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[api]"                           # CLI + server + dashboard
cb --version
```

Extras: `api` (server and dashboard), `postgres` (pgvector backend), `otel` (tracing), `all`, `dev` (everything plus pytest and ruff). The bare install gives you the CLI with SQLite storage.

## Quick start

### Command line

```bash
cb import experiments/sample_chat.txt -o design           # 1. extract (offline, redaction on)
cb inspect design --ids                                   # 2. review; ids drive the next commands
cb retrieve design "vector search decisions" --top-k 3    # 3. see what a task would pull in, and why
cb prompt design --model claude -q "vector search decisions" --copy   # 4. paste-ready prompt

cb share design <ITEM_ID> --policy local_only             # keep one item away from cloud models
cb audit design                                           # which items went to which model
cb health design                                          # stale items, open tasks, conflicts
cb serve                                                  # dashboard + API at http://127.0.0.1:5000
```

Step 3 prints real scores, not a black box:

```text
┌───┬───────┬────────────┬──────────────────────────────────────┬────────────────────────────┬─────┐
│ # │ Score │ Category   │ Memory                               │ Why                        │ Tok │
├───┼───────┼────────────┼──────────────────────────────────────┼────────────────────────────┼─────┤
│ 1 │  0.28 │ Decisions  │ I decided to use Pydantic for data   │ Matched 'vector', 'search' │  18 │
│   │       │            │ models and FAISS for vector search.  │ (lexical 0.28)             │     │
│ 2 │  0.12 │ Open tasks │ Each memory should belong to one of  │ Matched 'decision'         │  26 │
│   │       │            │ six categories: …                    │ (lexical 0.12)             │     │
└───┴───────┴────────────┴──────────────────────────────────────┴────────────────────────────┴─────┘
2 of 11 considered · method: lexical · ≈ 44 tokens selected, ≈ 184 saved vs. full memory (228)
```

`cb --help` lists every command and `cb <command> --help` every flag. Other commands worth knowing: `done`, `supersede` and `conflicts` for keeping memory true as decisions change; `merge` to combine packages with duplicate folding; `export-package` and `import-package` for portable JSON files; `scan` to preview redaction.

### Python

The CLI, server and extension are thin shells over `ContextBridgeService`. This runs from the repository root with no API key:

```python
from pathlib import Path

from contextbridge.config import Settings
from contextbridge.models import RetrievalOptions
from contextbridge.service import ContextBridgeService
from contextbridge.storage import get_store

settings = Settings.from_env()
service = ContextBridgeService(get_store(settings=settings), settings=settings)

text = Path("experiments/sample_chat.txt").read_text(encoding="utf-8")
outcome = service.import_text(text, "design", engine="local", origin="sample_chat.txt")
print(f"{outcome.added_items} items extracted, {outcome.redaction.total} values redacted")

result = service.retrieve("design", "vector search decisions", RetrievalOptions(top_k=3))
for hit in result.selected:
    print(f"{hit.score:.2f}  [{hit.item.category}]  {hit.item.content}  <- {hit.matched_terms}")

service.set_sharing("design", [result.selected[0].item.id], "local_only")
built = service.build_prompt("design", "claude", query="vector search decisions")
print(f"{built['included_items']} rendered, {built['withheld_count']} withheld")
print(built["prompt"])
print(service.audit("design")["summary"])
```

### HTTP

With `cb serve` running, the same operations are one request each. `GET /docs` has the full schemas.

```bash
curl -s http://127.0.0.1:5000/api/v1/extract -F "files[]=@experiments/sample_chat.txt" -F "package_name=design"
curl -s http://127.0.0.1:5000/api/v1/retrieve -H "Content-Type: application/json" \
     -d '{"package_name":"design","query":"vector search decisions","top_k":3}'
curl -s http://127.0.0.1:5000/api/v1/prompt -H "Content-Type: application/json" \
     -d '{"package_name":"design","target_model":"claude","query":"vector search decisions"}'
```

### Chrome extension

Run `cb serve`, open `chrome://extensions`, enable *Developer mode*, *Load unpacked*, and pick the [extension/](extension/) folder. On chatgpt.com or claude.ai a floating button extracts the open chat into a package and hands back a prompt for the other model. The extension talks to `localhost:5000`.

## Configuration

Everything is optional. Copy [.env.example](.env.example) to `.env` for the full, commented list. The ones that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `CB_STORAGE_DIR` | `~/.contextbridge` | Where the SQLite database and attached files live |
| `CB_DATABASE_URL` | — | PostgreSQL DSN; setting it switches storage to Postgres + pgvector |
| `CB_REDACT` | `true` | Mask secrets and PII before storing |
| `CB_DEFAULT_ADAPTER` | `local` | Extraction engine: `local`, `openai`, `claude`, or an Ollama model name |
| `CB_HOST` / `CB_PORT` | `127.0.0.1` / `5000` | Server bind address |
| `CB_API_KEY` | — | Protects the API when exposed beyond loopback (`X-API-Key` or `Authorization: Bearer`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Turns on OpenTelemetry tracing (names and counts only, never content) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Only needed for the matching engine or query target |

## Running as a service

The same code runs as a shared server on PostgreSQL + pgvector, where embeddings are persisted per package and the egress ledger lives in a table.

```bash
docker compose up --build                            # app + pgvector Postgres on :8000, /docs for OpenAPI
docker compose --profile observability up --build    # + OpenTelemetry collector and Jaeger on :16686

# or against your own database
export CB_DATABASE_URL=postgresql://user:pass@host:5432/contextbridge
cb db upgrade                                        # schema migrations
cb migrate --to postgres                             # copy local SQLite memory across; nothing deleted
CB_API_KEY=$(openssl rand -hex 24) cb serve --host 0.0.0.0 --port 8000
```

[infra/aws/](infra/aws/) provisions ECR, RDS with pgvector, App Runner and an OIDC deploy role with Terraform; [deploy.yml](.github/workflows/deploy.yml) ships every `main` commit and smoke-tests the live URL. Details and an operations checklist are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Privacy

- Local-first: SQLite under your home directory, server bound to loopback, no external fonts or scripts in the dashboard.
- Redaction runs before storage. Items record only the kind and count of what was masked.
- Nothing leaves the machine unless you choose a vendor engine or `cb query`, and both are recorded in the ledger.
- Logs and traces carry names and counts, never transcript text, memory content, prompts or keys. Tests assert this.

The full model is in [docs/PRIVACY.md](docs/PRIVACY.md).

## Quality

Two offline harnesses gate CI on every push. The golden retrieval harness scores 84 hand-written query → item pairs over six persona memory sets and fails the build if a metric drops more than 0.02 below the published baseline. At v0.4.0 with lexical retrieval: Hit@1 77%, Recall@3 84%, MRR 82% overall, and 100% on queries that share vocabulary with the memory. The `semantic` slice sits at 30% by design, to measure the headroom embeddings add. The component suite covers extraction, redaction (100% recall, 0 false positives), duplicate detection and token savings. What the numbers do and do not mean: [docs/EVALUATION.md](docs/EVALUATION.md).

<!-- BENCH:START -->
Benchmarks run on a GitHub `ubuntu-latest` runner so they name their hardware. A 100-item package round-trips in 1.2 ms on SQLite and 3.6 ms on Postgres; over 50k 384-dimensional vectors a pgvector HNSW query returns in 1.9 ms with 0.99 recall against exact search, and persisted embeddings halve the cost of a hybrid query. Tables and interpretation: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
<!-- BENCH:END -->

## Project layout

```text
contextbridge/
├── service.py        # ContextBridgeService: the one place the pieces are wired together
├── models.py         # Pydantic schema: MemoryItem, ContextPackage, EgressRecord, retrieval models
├── config.py         # Settings from CB_* / OTEL_* environment variables
├── cli.py            # Click CLI (`cb`)
├── core/             # extraction, redaction, lifecycle, sharing, packaging, retrieval, merging, prompt rendering
├── adapters/         # OpenAI, Claude and Ollama behind one LLMInterface
├── storage/          # SQLite (default), PostgreSQL + pgvector, legacy JSON, portable export
├── api/              # FastAPI app, routes, API-key dependency
├── evaluation/       # offline component suite, golden harness, fixtures and baseline
└── web/              # dashboard served by FastAPI (+ legacy Flask app)
tests/                # 297 tests: API, CLI, core, storage, Postgres, telemetry, golden harness
benchmarks/           # bench.py and published results
docs/                 # ARCHITECTURE · DEPLOYMENT · PRIVACY · EVALUATION · OBSERVABILITY · BENCHMARKS
extension/            # Chrome MV3 content script
infra/aws/            # Terraform for ECR, RDS pgvector, App Runner, OIDC
.github/workflows/    # CI (lint, tests on 3.11–3.13, eval, benchmarks, Docker e2e), deploy, benchmarks
```

## Development

```bash
pip install -e ".[dev]"
pytest -q --cov=contextbridge --cov-fail-under=80           # Postgres tests run when CB_TEST_DATABASE_URL is set
ruff check contextbridge tests benchmarks scripts && ruff format --check contextbridge tests benchmarks scripts
cb eval && cb eval --golden --check-baseline                 # the harnesses CI runs
docker compose up --build -d --wait && python scripts/e2e.py --base-url http://localhost:8000
```

Add behaviour to `ContextBridgeService` first and expose it through the CLI and API second. Every persisted field gets a default so older packages keep loading. Nothing that could be memory content, a key or a DSN password may be logged, traced or returned in an error. Retrieval changes must keep the golden gate green or update the baseline deliberately. Record user-facing changes in [CHANGELOG.md](CHANGELOG.md).

## Roadmap

Next up: an MCP server so Claude Desktop, Claude Code and Cursor can read and write memory live with sharing boundaries enforced; mid-thread hand-off packs when a context window fills; local sentence-transformer embeddings so the offline path is not purely lexical; and team packages with per-member boundaries.

## License

MIT — see [LICENSE](LICENSE).
