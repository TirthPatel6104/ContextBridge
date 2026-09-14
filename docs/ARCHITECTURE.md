# Architecture

ContextBridge treats conversational context as **data with a schema** rather than a blob of chat history. This document describes the moving parts, the data model, and the decisions behind them.

## Overview

```mermaid
graph TB
    subgraph Surfaces
        CLI["cb CLI (Click + Rich)"]
        WEB["Dashboard (Flask, create_app)"]
        EXT["Chrome extension (content script)"]
    end

    SVC["ContextBridgeService<br/>(service.py — one facade, injected store)"]

    subgraph Core["core/"]
        FP["file_parser"]
        LE["local_extractor (rules)"]
        ME["memory (LLM extractor)"]
        RED["redaction"]
        PK["packager (versions, diffs, rollback)"]
        RET["retriever (BM25 + optional embeddings)"]
        MRG["merger (duplicates, conflicts)"]
        PB["prompt_builder"]
        TOK["tokens (estimates)"]
    end

    subgraph Adapters["adapters/ (LLMInterface)"]
        OA["OpenAI"]
        CA["Claude"]
        LA["Ollama"]
    end

    subgraph Storage["storage/"]
        SQL["SQLiteStore (default)"]
        JS["JSONStore (legacy)"]
        PORT["portable (export/import envelope)"]
        VS["VectorStore (FAISS / numpy)"]
    end

    EVAL["evaluation/ (fixtures + runner)"]

    EXT -->|HTTP, allowed origins only| WEB
    CLI --> SVC
    WEB --> SVC
    SVC --> FP & LE & ME & RED & PK & RET & MRG & PB & TOK
    ME --> OA & CA & LA
    RET -.->|semantic embeddings only| OA & LA
    RET --> VS
    SVC --> SQL & JS & PORT
    EVAL --> LE & RET & MRG & RED
```

### Layers

| Layer | Responsibility | Key rule |
|---|---|---|
| **Surfaces** (`cli.py`, `web/app.py`, `extension/`) | Parse input, render output | No business logic; everything goes through the service |
| **Service** (`service.py`) | Orchestrates extraction → redaction → packaging → storage → retrieval → prompt | Takes the store and settings by injection; no module-level state |
| **Core** (`core/`) | Pure domain logic | No I/O except through the adapter interface; deterministic where possible |
| **Adapters** (`adapters/`) | Vendor SDK calls | Implement `LLMInterface`; declare whether embeddings are semantic |
| **Storage** (`storage/`) | Persistence | Validate names before touching disk; never delete history |

The web app is created by `create_app(settings, store=..., service=...)`, and the CLI builds a per-invocation `Context`. Tests inject a temporary SQLite store and a mock adapter, so the full workflow runs without network access.

## Data model

All models live in `models.py` (Pydantic v2).

```
ContextPackage
├── name, version, schema_version, source_model, created_at, updated_at
├── memory: StructuredMemory
│   ├── identity:    [MemoryItem]
│   ├── projects:    [MemoryItem]
│   ├── facts:       [MemoryItem]
│   ├── decisions:   [MemoryItem]
│   ├── open_tasks:  [MemoryItem]
│   └── preferences: [MemoryItem]
├── history: [ContextVersion{version, timestamp, diff{added, removed}, source_model, note}]
└── metadata: {}   # e.g. merged_from, merge_report

MemoryItem
├── id           # sha1(category + normalised content)[:12] — stable across re-extraction
├── category, content, confidence (0–1)
├── source       # excerpt from the conversation that supports the item
├── origin       # provenance: file name, chat title, or package name(s)
└── redactions   # [{kind, count}] — what was masked, never the value
```

**Why content-derived ids.** Diffs, merges, review removals, and evaluation all need to refer to "the same statement" across runs. Hashing the normalised content (plus category) gives a stable handle without a registry, and makes exact duplicates trivially detectable.

**Schema versions.** Packages written by 0.1.x lack `id`, `origin`, `redactions`, and `schema_version`. Every new field has a default, and ids are computed in a model validator, so old files load unchanged and are upgraded the next time they are saved.

### Version history

`ContextVersion.version` is the version the entry *produced* (v1 = "created", v2 = first update, ...). Rollback walks history backwards, removing the `added` items and restoring the `removed` ones. Because items are matched by id, reordering or whitespace changes never register as edits.

## Storage

### SQLite (default)

`storage/sqlite_store.py` keeps one database file, `contextbridge.db`, in the storage directory:

| Table | Purpose |
|---|---|
| `packages` | One summary row per package (current version, item count, timestamps). Listings never deserialise payloads. |
| `package_versions` | Full JSON payload of every saved version, keyed by `(name, version)`. Identical to the portable format, so export and rollback are trivial. |

Writes are transactional and guarded by a lock so the Flask dev server's threads cannot interleave. WAL mode is enabled.

### JSON (legacy, still supported)

`storage/json_store.py` writes `context_vN.json` files plus a `latest.json` copy per package. It is kept for backward compatibility and because the files are easy to inspect by hand. Writes are atomic (temp file + rename).

### Migration

`get_store()` picks the backend from `CB_STORAGE_BACKEND` (default `sqlite`). When SQLite is used, any legacy JSON package found in the storage directory that is not yet in the database is imported (all versions), and the JSON files are left in place. `cb migrate` runs the same routine explicitly and reports what happened.

### Portable packages

`storage/portable.py` wraps a package in a small envelope (`format`, `schema_version`, `exported_at`, `package`). Import accepts the envelope or a bare legacy package document, enforces a size limit, validates the name, and refuses to overwrite unless asked.

## Retrieval

`core/retriever.py` scores every item and returns a `RetrievalResult` that the UI can explain:

1. **Lexical scorer (always on).** BM25 over item content and source excerpt, with a small conservative stemmer and a stop-word list. Scores are normalised against the ceiling a document would reach if every query term were saturated, so `1.0` means "all query terms strongly present" and scores are comparable across queries. Matched terms are recorded per item.
2. **Embedding scorer (optional).** If the adapter sets `semantic_embeddings = True` (OpenAI, Ollama embedding models) and a `VectorStore` is provided, cosine similarity is blended in with weight 0.5. Claude's hash-based fallback is flagged non-semantic and ignored, so noise never outranks real matches. Any embedding failure degrades to lexical only.
3. **Selection.** Candidates are filtered by category, sorted by score (ties: confidence, category order, id), pruned by `min_score`, capped at `top_k`, and packed greedily under an optional `token_budget`. Items with zero evidence are never selected. Counters record what was dropped and why.
4. **Token accounting.** `core/tokens.py` estimates tokens (≈4 chars/token blended with a word count). The result reports tokens in the selection, in the full memory, and in the source transcript when known.

## Redaction

`core/redaction.py` runs a prioritised list of regex detectors (private keys, API keys, JWTs, credential assignments, credential URLs, card numbers with a Luhn check, SSNs, emails, phones, IP addresses). Overlaps are resolved by priority. Each masked span becomes `[REDACTED:KIND]` and the item records `{kind, count}`. The item id is recomputed after redaction. Policies can disable redaction or restrict it to certain kinds.

## Merging

`core/merger.py` folds near-duplicates within each category using a blend of token Jaccard and character ratio (threshold 0.82). The most confident, longest phrasing is kept and all origins are joined (`chat_a | chat_b`). Pairs in conflict-prone categories with similarity between 0.45 and 0.82 that share at least two terms are flagged as *possible conflicts* and both are kept. The threshold is deliberately high: a false negative leaves two items for the user to read, a false positive silently loses information.

## Prompt building

`core/prompt_builder.py` renders memory as XML (Claude), Markdown (ChatGPT), or plain numbered text (local models), adds a provenance footer (package, version, item counts, retrieval method), and wraps it in the paste-ready message used by the dashboard and extension. Attached files are included only when explicitly requested.

## Security posture of the local server

* Binds to `127.0.0.1` by default.
* CORS is restricted to the chat sites the extension runs on (`CB_ALLOWED_ORIGINS`).
* Package names must match `^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$`; upload file names are sanitised and restricted to known extensions; uploads are capped (`CB_MAX_UPLOAD_MB`).
* Unexpected exceptions return a generic message; details go to the server log. Memory content, transcripts, and API keys are never logged.

## Evaluation

`evaluation/` ships JSON fixtures (three labelled transcripts, duplicate pairs, redaction samples) and a runner that computes extraction coverage, retrieval precision/recall/MRR, duplicate F1, redaction recall and false positives, and token savings. See [EVALUATION.md](EVALUATION.md).

## Extending

* **New adapter:** subclass `LLMInterface`, set `semantic_embeddings`, register in `adapters/get_adapter`.
* **New storage backend:** subclass `StorageBackend`, validate names, add to `storage/get_store`.
* **New detector:** append a `Detector` to `redaction.DETECTORS` and a sample to `evaluation/fixtures/redaction.json`.
* **New fixture:** add a transcript JSON under `evaluation/fixtures/` and list it in `index.json`.
