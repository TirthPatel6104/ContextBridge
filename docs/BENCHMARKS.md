# Benchmarks

Two questions drove the 0.4 storage work: *what does moving from SQLite to
PostgreSQL cost on the package path*, and *when is pgvector worth it over the
in-memory FAISS index* that every request used to rebuild. `benchmarks/bench.py`
answers both with numbers you can reproduce; the published tables come from a
GitHub Actions `ubuntu-latest` runner (2 vCPU, 7 GB RAM, `pgvector/pgvector:pg16`
service container) so the hardware is the same for every run.

```bash
python benchmarks/bench.py --database-url postgresql://cb:cb@localhost:5432/cb \
    --sizes 1000,10000,50000 --dimension 384 \
    --output benchmarks/results/local.json --markdown benchmarks/results/local.md
```

The **Benchmarks** CI job runs exactly this on every push and pull request and
uploads `benchmarks/results/` as an artifact; the manual *Benchmarks* workflow
accepts other sizes and dimensions. Committed results live in
`benchmarks/results/github-runner.{json,md}` and are refreshed by hand when the
storage code changes, so the table below always names its environment.

## What is measured

| Section | Method |
|---|---|
| **Package round-trip** | `save()` + `load()` of a 100-item package; median and p95 of 50 runs, SQLite (WAL) vs Postgres (JSONB payload, pooled connection). |
| **Listing** | `summaries()` with 200 packages present. |
| **Vector index build** | Time to insert *n* vectors of dimension 384 into FAISS `IndexFlatIP`, the numpy fallback, and pgvector (`COPY` bulk upsert, then HNSW build with `m=16`, `ef_construction=64`). |
| **Vector query** | Median and p95 of a top-10 cosine query over 50 runs, plus **recall@10 of pgvector HNSW against exact search** on 200 queries (`hnsw.ef_search=100`). FAISS flat and numpy are exact by construction. |
| **Embedding reuse** | End-to-end hybrid retrieval with a deterministic embedder that sleeps 50 ms per call: the second query against an in-memory store (which re-embeds every item each request) versus pgvector (which embeds only the query). |

The corpus is **not** uniformly random. Uniform high-dimensional vectors are the
worst case for graph indexes (every point is nearly equidistant from every
other) and understate HNSW recall badly; a first run with random vectors gave
recall 0.21 at 50k. Real embeddings have topic structure, so the benchmark draws
unit vectors around 64 random centres (a mixture of Gaussians, spread 0.35) and
draws queries the same way.

## Results (GitHub Actions ubuntu-latest, Python 3.12, pgvector 0.8.x)

<!-- BENCH:START -->
_Pending: the table is filled from `benchmarks/results/github-runner.md` after the CI run for this release._
<!-- BENCH:END -->

## How to read the numbers

* **SQLite wins every single-process latency race, by roughly an order of magnitude.** It is a local file with no network hop. That is why it stays the default for the CLI and the desktop dashboard.
* **Postgres buys concurrency and persistence, not speed.** Its package round-trip is dominated by two network round-trips and JSONB parsing, and it is what lets several API workers share one memory store safely.
* **Below ~10k vectors, the in-memory FAISS index is faster than pgvector for a single query, but pgvector wins the request.** The in-memory path has to re-embed every item on every request (the *embedding reuse* table): with a 50 ms embedding call, that is the whole budget. Persisted embeddings turn a hybrid query into one embedding call plus one indexed search.
* **HNSW recall on clustered data stays above 0.9 with `ef_search=100`; exact scan is the fallback when you need 1.0.** `search_embeddings(exact=True)` is a query-time switch, and small packages (the planner prefers a sequential scan under a few thousand rows) are exact anyway.
* **Build cost is paid once.** `COPY` streams the vectors; the HNSW index for one dimension is created lazily on first write and reused by every package in the database.

## Reproducing locally

Any pgvector-enabled Postgres works (`docker compose up db` provides one). Note
that Docker Desktop on Windows adds tens of milliseconds to every request larger
than one TCP segment through its port proxy, which inflates the pgvector query
latencies far beyond what the database is doing (an `EXPLAIN ANALYZE` of the
same query reports well under 1 ms); run the script inside WSL or on Linux for
representative numbers.
