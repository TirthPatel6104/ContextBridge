<p align="center">
  <h1 align="center">🌉 ContextBridge</h1>
  <p align="center"><strong>Portable, privacy-conscious working memory for people who move between ChatGPT, Claude, and local models.</strong></p>
  <p align="center">Extract structured memory from a conversation, review and redact it, retrieve only what the next task needs, and paste it into any model — with every selection explained.</p>
</p>

<p align="center">
  <a href="https://github.com/TirthPatel6104/ContextBridge/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TirthPatel6104/ContextBridge/actions/workflows/ci.yml/badge.svg" /></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white" />
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green" />
  <img alt="Tests" src="https://img.shields.io/badge/tests-201%20offline-brightgreen" />
  <img alt="Version" src="https://img.shields.io/badge/version-0.2.0-purple" />
</p>

---

## The problem

You spend an afternoon in ChatGPT designing a database schema. The next morning you open Claude to write the migration, and it knows nothing: not your name, not the stack, not the three decisions you already argued through, not the two tasks still open. So you re-explain — badly, from memory, burning tokens on a wall of pasted chat that is mostly noise.

Every AI tool keeps its own memory, none of them share it, and none of them let you see or edit what they remember. Pasting whole transcripts is the workaround, and it leaks secrets, wastes context windows, and still loses the thread.

## What ContextBridge does

ContextBridge turns a conversation into **reviewable, portable, structured memory** that you own:

| Step | What happens | Why it matters |
|---|---|---|
| **1. Import** | A transcript, ChatGPT export ZIP, saved page, PDF, or Markdown is parsed locally and decomposed into six categories: identity, projects, facts, decisions, open tasks, preferences. Works fully offline with a rule-based extractor, or with an LLM when you want quality over privacy. | Memory becomes data with a schema, not a blob. |
| **2. Review & redact** | Every item shows its confidence, the source excerpt it came from, and where it came from. Secrets and personal data are masked before storage, transparently. You remove what is wrong; removals are versioned and reversible. | You see and control exactly what will be carried forward. |
| **3. Retrieve** | Type what you are about to do. Items are scored, and the result shows *which* were selected, their scores, the matched terms, and the estimated tokens saved compared with the full memory or the raw transcript. Tune top-k, categories, minimum score, and a token budget. | No black-box retrieval; no wasted context window. |
| **4. Export** | The selection is rendered the way the target model prefers (XML for Claude, Markdown for ChatGPT, plain text for Ollama) with a provenance footer, ready to paste. Packages export to a single portable JSON file. | Continuity across tools without vendor lock-in. |

Plus **merging**: combine several conversations into one working memory. Near-duplicates are folded with their origins preserved; statements that *might* conflict ("deadline is Q3" vs "deadline is Q4") are flagged and both kept for you to decide.

Everything runs on `127.0.0.1`. The only time a transcript leaves your machine is when you explicitly choose a vendor extraction engine. See [docs/PRIVACY.md](docs/PRIVACY.md).

### Who it is for

* **Developers** carrying architecture decisions and open TODOs between a coding assistant and a chat model.
* **Researchers and students** keeping experiment constraints, citation preferences, and reading notes consistent across tools.
* **Knowledge workers** who plan in one assistant and draft in another and are tired of re-briefing.

## Quick start

```bash
git clone https://github.com/TirthPatel6104/ContextBridge.git
cd ContextBridge
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,web]"
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
```

Then start the dashboard for the same workflow with a UI:

```bash
python -m contextbridge.web.app        # → http://127.0.0.1:5000
```

### Demo walkthrough (dashboard)

1. Drop `experiments/sample_chat.txt` on **step 1**, keep *Redact secrets* on, click **Extract memory**. The result shows how many items were extracted and what, if anything, was redacted.
2. **Step 2** loads the package: each item has a category chip, a confidence bar, its source excerpt, and its origin. Tick a wrong item and click **Remove selected** — the history table gains a new version with a roll-back button.
3. In **step 3** type `vector search` and click **Preview retrieval**. Each selected item shows its score, the matched terms, and a one-line reason; the stat row shows estimated tokens in the selection versus the full memory and the transcript.
4. In **step 4** choose *Claude*, scope *Only the retrieval selection*, click **Generate prompt**, then **Copy**. Paste it as your first message in Claude.
5. Import a second transcript under another name, then use **Merge packages** with *Preview merge* to see folded duplicates and flagged conflicts before saving.

Screenshots and a GIF belong in [docs/screenshots/](docs/screenshots/README.md) (placeholders describe what to capture).

## Three surfaces, one engine

| Surface | Use it for |
|---|---|
| **`cb` CLI** | Scripted or terminal workflows: import, retrieve, prompt, merge, export/import package files, evaluation |
| **Dashboard** (`127.0.0.1:5000`) | The guided import → review → retrieve → export workflow, package tools, and the evaluation panel |
| **Chrome extension** | A floating button on chatgpt.com / claude.ai that sends the open conversation to your local server and hands back a prompt for the other model |

All three call the same `ContextBridgeService`, so behaviour is identical.

## Architecture

```mermaid
graph LR
    subgraph Surfaces
        CLI["cb CLI"]
        WEB["Flask dashboard"]
        EXT["Chrome extension"]
    end
    SVC["ContextBridgeService"]
    subgraph Core
        EXTR["Extractors<br/>(rules / LLM)"]
        RED["Redaction"]
        PKG["Packager<br/>(versions, diffs)"]
        RET["Retriever<br/>(BM25 + optional embeddings)"]
        MRG["Merger<br/>(duplicates, conflicts)"]
        PB["Prompt builder"]
    end
    subgraph Storage
        SQL["SQLite (default)"]
        JSON["JSON (legacy)"]
        PORT["Portable export"]
    end
    EXT -->|allowed origins only| WEB
    CLI --> SVC
    WEB --> SVC
    SVC --> EXTR --> RED --> PKG --> SQL
    SVC --> RET
    SVC --> MRG
    SVC --> PB
    PKG --> JSON
    PKG --> PORT
```

Design notes, the data model, and the storage schema are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Data model in one glance

```json
{
  "name": "contextbridge_design",
  "version": 2,
  "schema_version": 2,
  "memory": {
    "decisions": [
      {
        "id": "9c1f3e2a7b4d",
        "category": "decisions",
        "content": "Using Pydantic for data models and FAISS for vector search",
        "confidence": 0.7,
        "source": "rule:decision_keyword",
        "origin": "sample_chat.txt",
        "redactions": []
      }
    ]
  },
  "history": [
    {"version": 1, "note": "created", "diff": {"added": ["…"], "removed": []}},
    {"version": 2, "note": "removed during review", "diff": {"added": [], "removed": ["…"]}}
  ]
}
```

Item ids are derived from category + normalised content, so the same statement gets the same id across re-extractions, diffs, and merges.

## Privacy model

* **Local-first by default.** Storage is a SQLite file under `~/.contextbridge`. The dashboard loads no external fonts or scripts and binds to loopback. CORS is limited to the chat sites the extension runs on.
* **Redaction before storage.** API keys, tokens, JWTs, private keys, credential assignments, card numbers, SSNs, emails, phone numbers, and IP addresses are replaced with `[REDACTED:KIND]` placeholders. The item records only the kind and count. You see the report; your original files are never modified; you can opt out per import.
* **Nothing is logged that you would not want in a log.** Counts and package names, yes; transcript text, memory content, prompts, and keys, no.
* **Explicit egress only.** Choosing `openai` or `claude` as an extraction engine sends the transcript to that vendor. `cb query` sends the question and selected memory to the model you name. Nothing else leaves the machine.

Full details: [docs/PRIVACY.md](docs/PRIVACY.md).

## How it evaluates itself

`cb eval` runs an offline suite over bundled fixtures (three labelled transcripts, duplicate pairs, redaction samples). No API calls, deterministic, about a second.

| Metric (v0.2.0) | Value |
|---|---|
| Extraction coverage (rule-based extractor) | 100.0% |
| Retrieval precision@3 / recall@3 / MRR | 76.7% / 93.3% / 86.7% |
| Duplicate detection F1 | 75.0% |
| Redaction recall / false positives | 100.0% / 0 |
| Token savings vs. transcript / vs. full memory | 93.2% / 81.5% |

These numbers describe the offline components on friendly fixtures. Extraction coverage is high *because* the fixtures use explicit phrasing the rules understand; the duplicate score is deliberately conservative (false negatives become flagged conflicts rather than silent merges); one retrieval query fails for the textbook lexical reason (no shared vocabulary). [docs/EVALUATION.md](docs/EVALUATION.md) explains each metric, the misses, and how to add fixtures.

## Configuration

Copy `.env.example` to `.env`. Everything is optional.

| Variable | Default | Purpose |
|---|---|---|
| `CB_STORAGE_DIR` | `~/.contextbridge` | Where packages live |
| `CB_STORAGE_BACKEND` | `sqlite` | `sqlite` or `json` (legacy per-version files) |
| `CB_REDACT` | `true` | Redact secrets/PII before storing |
| `CB_DEFAULT_ADAPTER` | `local` | Default extraction engine |
| `CB_HOST` / `CB_PORT` | `127.0.0.1` / `5000` | Dashboard bind address |
| `CB_ALLOWED_ORIGINS` | chatgpt.com, chat.openai.com, claude.ai | Browser origins allowed to call the API |
| `CB_MAX_UPLOAD_MB` | `25` | Upload size limit |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | — | Only for the matching engine / query target |

Extraction engines: `local` (offline rules), `openai`, `claude`, or any Ollama model name (e.g. `--engine llama3`).

## CLI reference

| Command | What it does |
|---|---|
| `cb import FILES… -o NAME [--engine E] [--no-redact] [--mode append\|replace]` | Extract memory from transcripts / exports / PDFs / Markdown |
| `cb retrieve NAME "query" [-k N] [-c category]… [--budget T] [--min-score S] [--json]` | Explainable retrieval with token savings |
| `cb prompt NAME --model claude\|openai\|local [-q "query"] [--copy] [--raw]` | Paste-ready prompt, optionally scoped by retrieval |
| `cb query "question" -c NAME -m MODEL` | Ask a model with relevant memory injected (calls the API) |
| `cb list` / `cb inspect NAME [--ids]` / `cb history NAME` | Browse packages, items, and versions |
| `cb remove NAME ID…` / `cb rollback NAME -v N` / `cb delete NAME` | Curate and undo |
| `cb export-package NAME [-o FILE]` / `cb import-package FILE [--name N] [--overwrite]` | Portable JSON files |
| `cb merge A B… -o NEW [--dry-run]` | Merge with duplicate folding and conflict flags |
| `cb scan FILES…` | Preview what redaction would mask, without storing anything |
| `cb eval [--json]` | Offline evaluation suite |
| `cb info` / `cb migrate` | Storage details; import legacy JSON packages into SQLite |

`cb export` and `cb web-export` remain as aliases from 0.1.

## Chrome extension

1. Start the dashboard (the extension talks to `localhost:5000`).
2. `chrome://extensions` → *Developer mode* → *Load unpacked* → select `extension/`.
3. On chatgpt.com or claude.ai, click the floating button, name the package, and click **Extract this chat**. The status line reports how many items were extracted and what was redacted. **Copy prompt** gives you the prompt for the other model; files you attach in the panel are included because you attached them, nothing else is.

## Development

```bash
pytest -q                              # 201 tests, all offline (mock adapter, temp SQLite)
ruff check contextbridge tests         # lint
ruff format --check contextbridge tests
cb eval                                # evaluation suite
```

CI runs all four on Python 3.11, 3.12, and 3.13.

Project layout:

```
contextbridge/
├── models.py            # Pydantic schema: MemoryItem (id, origin, redactions), ContextPackage, RetrievalResult
├── config.py            # Settings from CB_* env vars
├── validation.py        # Package names, file names, sizes
├── service.py           # ContextBridgeService: the one place the pieces are wired
├── cli.py               # Click CLI
├── core/                # extractors, redaction, packager, retriever, merger, prompt_builder, tokens
├── adapters/            # OpenAI, Claude, Ollama (LLMInterface)
├── storage/             # SQLiteStore, JSONStore, portable export/import, VectorStore
├── evaluation/          # runner + fixtures
└── web/                 # Flask app factory, templates, static
extension/               # Chrome MV3 content script
tests/                   # 201 tests
docs/                    # ARCHITECTURE, PRIVACY, EVALUATION, screenshots
```

## Upgrading from 0.1

Nothing to do for most users. On first run the SQLite backend imports every JSON package (all versions) from `~/.contextbridge` and leaves the JSON files in place. `cb migrate` runs the same import explicitly and reports what it did; `cb info` shows where data lives. `CB_STORAGE_BACKEND=json` keeps the old behaviour. Note that `--engine local` / `--model local` for *extraction* now means the offline rule-based extractor everywhere (the dashboard already worked this way); use an Ollama model name for local LLM extraction. Details in [CHANGELOG.md](CHANGELOG.md).

## Roadmap

Implemented in 0.2.0:

- [x] SQLite storage with automatic, non-destructive migration from JSON
- [x] Portable package export/import
- [x] Explainable retrieval: scores, matched terms, reasons, category filters, top-k, token budgets, token-savings estimates
- [x] Secrets/PII redaction with transparent reporting
- [x] Multi-package merge with duplicate folding and conflict flags
- [x] Review workflow with per-item provenance, versioned removals, rollback
- [x] Offline evaluation suite wired into CLI, API, dashboard, and CI
- [x] Input validation, restricted CORS, loopback binding, no content in logs

Planned (not yet implemented):

- [ ] Recorded-fixture evaluation of LLM extractors (grade saved outputs offline)
- [ ] Local semantic embeddings (e.g. a small sentence-transformer) so the offline path is not purely lexical
- [ ] Item editing in the review step (currently: remove, re-import, or merge)
- [ ] Streaming / chunked extraction for very long transcripts
- [ ] Extension: select which items to include before copying

## License

MIT — see [LICENSE](LICENSE).
