# Evaluation

ContextBridge ships a small, reproducible evaluation suite so that claims about
the offline components are measured, not asserted. It runs in about a second,
uses only bundled fixtures, and makes **no API calls**.

```bash
cb eval            # summary table
cb eval --json     # per-fixture and per-query detail
curl http://127.0.0.1:5000/api/eval   # same report from the dashboard server
```

CI runs it on every push.

## What is measured

| Section | Component under test | Metric | How |
|---|---|---|---|
| Extraction | `LocalExtractor` (offline rules) | Coverage of gold items; share of extracted items matching no gold item | A gold item counts as covered when one extracted item contains all of its keywords |
| Retrieval | `MemoryRetriever` (lexical BM25) | Precision@3, Recall@3, MRR | Queries run over the hand-labelled **gold** memory, so extraction errors do not leak into retrieval scores |
| Duplicates | `PackageMerger.similarity` at the default threshold | Precision, recall, F1 | 20 labelled pairs, half duplicates (rephrasings) and half near-misses on the same topic |
| Redaction | `redaction.scan_text` | Recall per kind; false positives | 17 samples with planted secrets/PII, 10 clean samples with version numbers, dates, ports, etc. |
| Tokens | `core.tokens` + retriever | Savings vs. transcript and vs. full memory | Estimated tokens of the transcript, of the whole gold memory, and of the top-3 selection averaged over the fixture's queries |

Fixtures live in `contextbridge/evaluation/fixtures/`:

* `backend_migration.json` — a developer planning a Flask → FastAPI/PostgreSQL migration
* `thesis_research.json` — a PhD student planning low-resource MT experiments
* `product_launch.json` — a product manager preparing a pricing launch
* `duplicates.json`, `redaction.json`

Each transcript fixture has 7 gold items across the six categories and 5 retrieval queries with their relevant gold indices. All content is synthetic.

## Current results (v0.2.0)

| Metric | Value |
|---|---|
| Extraction coverage (rule-based) | 100.0% |
| Extracted items not matching gold | 3.6% |
| Retrieval precision@3 | 76.7% |
| Retrieval recall@3 | 93.3% |
| Retrieval MRR | 86.7% |
| Duplicate detection F1 | 75.0% |
| Redaction recall | 100.0% |
| Redaction false positives | 0 |
| Token savings vs. transcript | 93.2% |
| Token savings vs. full memory | 81.5% |

Per-fixture token figures (estimates):

| Fixture | Transcript | Full memory | Mean top-3 selection |
|---|---|---|---|
| backend_migration | 289 | 94 | 21 |
| thesis_research | 263 | 96 | 15 |
| product_launch | 241 | 103 | 17 |

## How to read these numbers honestly

* **Extraction coverage is 100% because the fixtures are written the way the rule-based extractor expects** ("I decided…", "I need to…", "The constraint is…"). Real chats are messier; expect the offline extractor to miss implicit facts and to over-extract chatty sentences. Use an LLM engine when quality matters more than staying offline. The metric exists to catch regressions in the rules, not to advertise the extractor.
* **Retrieval misses are the interesting part.** Of 15 queries, 12 rank the gold item first. The three that do not:
  * *"which translation model did we decide to fine-tune"* ranks the project item ("machine translation for Ladin") above the decision ("Fine-tune NLLB-200…") because "translation" is a strong shared term — rank 2.
  * *"when is the launch date"* ranks "Building the launch plan…" above the dated decision — rank 2.
  * *"how should documents be formatted"* finds nothing: the gold item says "bullet-point summaries" and shares no vocabulary with the query. This is the classic lexical-retrieval failure that semantic embeddings fix; when an adapter with real embeddings is configured, ContextBridge blends them in.
* **Duplicate F1 is 75% by design choice.** All four errors are false negatives: rephrasings with similarity 0.70–0.79 sit below the 0.82 threshold. Lowering the threshold to catch them would also fold pairs like "Deadline is the end of Q3" / "…Q4" (0.73) and "citations in APA" / "…IEEE" (0.74), which is silent data loss. Those band pairs are surfaced as *possible conflicts* instead, so the user still sees them together.
* **Redaction recall is measured on formats the detectors know.** Unusual key formats, secrets split across lines, or spelled-out phone numbers will be missed. Zero false positives on the clean set does not mean zero in the wild: long digit strings that pass a Luhn check, for instance, would be flagged.
* **Token counts are estimates** (≈4 characters per token blended with a word count). Relative savings are meaningful; absolute numbers are ±20%.

## Limitations of the suite itself

* Three transcripts is enough to catch regressions, not to characterise performance. Adding fixtures is cheap (see below) and welcome.
* LLM-based extraction (OpenAI, Claude, Ollama) is not evaluated because it would require network access and non-deterministic model output. A future harness could record fixtures from a run and grade them offline.
* Retrieval is scored against gold memory, not extracted memory, so the end-to-end number a user experiences also depends on extraction quality.
* There is no user study; "helpfulness" of the injected context to the target model is not measured.

## Adding a fixture

1. Write a transcript with `User:` / `Assistant:` turns.
2. Label `gold_items` (category, content, and 1–2 `keywords` that must appear in a matching extracted item).
3. Add `queries` with `relevant_gold` indices into the `gold_items` list.
4. Register the file in `fixtures/index.json` and run `cb eval`.

Tests in `tests/test_evaluation.py` assert floors (MRR ≥ 0.7, redaction recall ≥ 0.9, zero false positives, duplicate F1 ≥ 0.7) so a regression fails CI.
