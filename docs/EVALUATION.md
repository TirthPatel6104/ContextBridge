# Evaluation

ContextBridge ships two reproducible, offline evaluation harnesses so that
claims about the retrieval and extraction components are measured, not
asserted. Both run in a few seconds, use only bundled fixtures, make **no API
calls**, and run in CI on every push and pull request.

| Harness | Command | What it covers | CI behaviour |
|---|---|---|---|
| **Component suite** | `cb eval` | Rule-based extraction coverage, retrieval on three labelled transcripts, duplicate detection, redaction, token savings | Report uploaded as an artifact; floors asserted by `tests/test_evaluation.py` |
| **Golden retrieval harness** | `cb eval --golden` | 84 hand-written query → relevant-item pairs over six persona memory sets, tagged by difficulty | `--check-baseline` fails the build when a guarded metric drops more than 0.02 below the published baseline |

Both are also served by the API (`GET /api/v1/eval`, `GET /api/v1/eval/golden`) and shown in the dashboard's evaluation panel.

## Golden retrieval harness

`contextbridge/evaluation/fixtures/golden_pairs.json` holds six synthetic memory sets of twelve items each (a fintech backend engineer, a PhD student, a product manager, a home renovation, a data-platform lead, a job search) and 84 queries. Every query lists the item indices a good retriever must surface and one or more tags:

| Tag | Meaning | Pairs |
|---|---|---|
| `lexical` | The query shares clear vocabulary with the relevant item | 46 |
| `paraphrase` | Reworded; some stems still overlap ("evaluation metrics" vs "Evaluate with chrF and COMET") | 28 |
| `semantic` | No meaningful vocabulary overlap ("how much can we spend" → "Total budget is £28,000") | 10 |
| `multi` | More than one item is relevant | 7 |

Metrics per pair: Hit@1, Precision@k, Recall@k, reciprocal rank, nDCG@k (k = 3 by default). The report aggregates them overall and per tag.

### Published baseline (v0.4.0, lexical BM25, k = 3)

| Slice | Pairs | Hit@1 | P@3 | R@3 | MRR | nDCG@3 |
|---|---|---|---|---|---|---|
| **all** | 84 | 77.4% | 30.6% | 84.1% | 82.1% | 81.5% |
| lexical | 46 | 100.0% | 34.1% | 98.9% | 100.0% | 99.2% |
| paraphrase | 28 | 57.1% | 32.1% | 79.2% | 71.4% | 71.0% |
| multi | 7 | 71.4% | 52.4% | 66.7% | 85.7% | 67.1% |
| semantic | 10 | 30.0% | 10.0% | 30.0% | 30.0% | 30.0% |

The baseline lives in `contextbridge/evaluation/fixtures/golden_baseline.json`. `cb eval --golden --check-baseline` compares the five guarded aggregate metrics against it with a tolerance of 0.02 and exits non-zero on a regression, which is what the CI job does. When retrieval legitimately improves, regenerate it with `cb eval --golden --update-baseline contextbridge/evaluation/fixtures/golden_baseline.json` and commit the change with the code that earned it.

How to read it:

* **Lexical pairs are at 100% Hit@1.** That is the floor a BM25 retriever must keep; a stemming or stop-word regression shows up here first.
* **Paraphrase is where the conservative stemmer costs recall.** "relocate" does not match "relocating", "plan" does not match "planning". Loosening the stemmer trades these for false matches; the harness makes that trade measurable rather than a matter of taste.
* **Semantic pairs are expected to fail under lexical retrieval.** They are in the set to quantify the headroom that real embeddings buy. `run_golden(adapter=…)` scores the hybrid path with any adapter that has semantic embeddings, and `tests/test_golden.py` runs it with the mock adapter to keep the code path covered. Publishing hybrid numbers requires a real embedding model and is left for a run with `OPENAI_API_KEY` or an Ollama embedding model set.
* **Precision@3 is low by construction** for single-answer queries: at most one of three slots can be right. Compare P@k across runs, not against 1.0.

## Component suite

| Section | Component under test | Metric | How |
|---|---|---|---|
| Extraction | `LocalExtractor` (offline rules) | Coverage of gold items; share of extracted items matching no gold item | A gold item counts as covered when one extracted item contains all of its keywords |
| Retrieval | `MemoryRetriever` (lexical BM25) | Precision@3, Recall@3, MRR | Queries run over the hand-labelled **gold** memory, so extraction errors do not leak into retrieval scores |
| Duplicates | `PackageMerger.similarity` at the default threshold | Precision, recall, F1 | 20 labelled pairs, half duplicates (rephrasings) and half near-misses on the same topic |
| Redaction | `redaction.scan_text` | Recall per kind; false positives | 17 samples with planted secrets/PII, 10 clean samples with version numbers, dates, ports, etc. |
| Tokens | `core.tokens` + retriever | Savings vs. transcript and vs. full memory | Estimated tokens of the transcript, of the whole gold memory, and of the top-3 selection averaged over the fixture's queries |

Fixtures live in `contextbridge/evaluation/fixtures/`: `backend_migration.json`, `thesis_research.json`, `product_launch.json` (7 gold items and 5 queries each), `duplicates.json`, `redaction.json`. All content is synthetic.

### Current results (v0.4.0)

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

### How to read these numbers honestly

* **Extraction coverage is 100% because the fixtures are written the way the rule-based extractor expects** ("I decided…", "I need to…", "The constraint is…"). Real chats are messier; expect the offline extractor to miss implicit facts and to over-extract chatty sentences. Use an LLM engine when quality matters more than staying offline. The metric exists to catch regressions in the rules, not to advertise the extractor.
* **Duplicate F1 is 75% by design choice.** All four errors are false negatives: rephrasings with similarity 0.70–0.79 sit below the 0.82 threshold. Lowering the threshold to catch them would also fold pairs like "Deadline is the end of Q3" / "…Q4" (0.73), which is silent data loss. Those band pairs are surfaced as *possible conflicts* instead.
* **Redaction recall is measured on formats the detectors know.** Unusual key formats, secrets split across lines, or spelled-out phone numbers will be missed.
* **Token counts are estimates** (≈4 characters per token blended with a word count). Relative savings are meaningful; absolute numbers are ±20%.

## Limitations

* LLM-based extraction (OpenAI, Claude, Ollama) is not evaluated because it would require network access and non-deterministic model output. A recorded-fixture harness (grade saved outputs offline, with an LLM judge and agreement statistics) is the next step and is tracked in the roadmap.
* Retrieval is scored against gold memory, not extracted memory, so the end-to-end number a user experiences also depends on extraction quality.
* There is no user study; "helpfulness" of the injected context to the target model is not measured.
* Vector search performance (latency, HNSW recall against exact search) is a separate concern, covered by [BENCHMARKS.md](BENCHMARKS.md).

## Adding golden pairs

1. Add items to an existing memory set or create a new one in `golden_pairs.json` (`category` + `content`).
2. Add pairs with a unique `id`, the `set`, the `query`, the `relevant` indices, and honest `tags`.
3. Run `cb eval --golden`; `validate_golden` rejects unknown sets, out-of-range indices and duplicate ids.
4. If the aggregate moves, update the baseline in the same commit and explain why in the pull request.
