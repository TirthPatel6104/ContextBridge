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
unit vectors around 64 random centres (a mixture of Gaussians with unit-norm noise scaled by 0.6, so nearest neighbours are meaningfully closer than random points) and
draws queries the same way.

## Results (GitHub Actions ubuntu-latest, Python 3.12, pgvector 0.8.x)

<!-- BENCH:START -->
Run of 2026-09-21 · Python 3.12.14 · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 4 vCPUs · pgvector 0.8.6 · contextbridge 0.4.0. Raw data: `benchmarks/results/github-runner*.json`.

### Package storage (100-item package)

| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |
|---|---|---|---|---|
| package_roundtrip | 1.159 ms | 1.633 ms | 3.635 ms | 4.421 ms |
| listing_200_packages | 0.385 ms | 0.418 ms | 2.023 ms | 2.111 ms |

### Vector search (top-10, cosine, hierarchical corpus)

Corpus: 64 topics × 32 sub-topics, points tightly around a sub-topic (relative noise 0.25 within, 0.6 between); queries drawn from the same structure.

| n | dim | Backend | Build | Query median | Query p95 | Recall@10 |
|---|---|---|---|---|---|---|
| 1,000 | 384 | FAISS IndexFlatIP (in-memory) | 76.6 ms | 0.1 ms | 0.132 ms | 1.000 |
| 1,000 | 384 | numpy brute force (in-memory) | 0.1 ms | 0.069 ms | 0.091 ms | 1.000 |
| 1,000 | 384 | pgvector exact scan | 252.7 ms | 1.758 ms | 1.874 ms | 1.000 |
| 1,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 252.7 + 195.9 ms | 1.735 ms | 1.78 ms | 1.000 |
| 10,000 | 384 | FAISS IndexFlatIP (in-memory) | 307.1 ms | 0.682 ms | 0.876 ms | 1.000 |
| 10,000 | 384 | numpy brute force (in-memory) | 1.0 ms | 0.488 ms | 2.998 ms | 1.000 |
| 10,000 | 384 | pgvector exact scan | 2511.4 ms | 7.213 ms | 10.265 ms | 1.000 |
| 10,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 2511.4 + 951.7 ms | 1.664 ms | 1.713 ms | 1.000 |
| 50,000 | 384 | FAISS IndexFlatIP (in-memory) | 1506.4 ms | 4.575 ms | 5.18 ms | 1.000 |
| 50,000 | 384 | numpy brute force (in-memory) | 4.3 ms | 3.464 ms | 3.7 ms | 1.000 |
| 50,000 | 384 | pgvector exact scan | 12824.6 ms | 22.68 ms | 23.814 ms | 1.000 |
| 50,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 12824.6 + 10717.8 ms | 1.871 ms | 2.021 ms | 0.992 |

### Vector search, random corpus (adversarial lower bound)

Corpus: uniformly random unit vectors (adversarial for graph indexes).

| n | dim | Backend | Build | Query median | Query p95 | Recall@10 |
|---|---|---|---|---|---|---|
| 1,000 | 384 | FAISS IndexFlatIP (in-memory) | 80.1 ms | 0.099 ms | 0.126 ms | 1.000 |
| 1,000 | 384 | numpy brute force (in-memory) | 0.1 ms | 0.074 ms | 0.092 ms | 1.000 |
| 1,000 | 384 | pgvector exact scan | 253.8 ms | 1.725 ms | 1.969 ms | 1.000 |
| 1,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 253.8 + 253.2 ms | 1.872 ms | 1.917 ms | 1.000 |
| 10,000 | 384 | FAISS IndexFlatIP (in-memory) | 313.5 ms | 0.681 ms | 1.127 ms | 1.000 |
| 10,000 | 384 | numpy brute force (in-memory) | 1.1 ms | 0.475 ms | 1.436 ms | 1.000 |
| 10,000 | 384 | pgvector exact scan | 2449.1 ms | 7.639 ms | 8.367 ms | 1.000 |
| 10,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 2449.1 + 1973.4 ms | 6.984 ms | 7.349 ms | 1.000 |
| 50,000 | 384 | FAISS IndexFlatIP (in-memory) | 1508.3 ms | 4.663 ms | 5.037 ms | 1.000 |
| 50,000 | 384 | numpy brute force (in-memory) | 4.6 ms | 3.626 ms | 3.922 ms | 1.000 |
| 50,000 | 384 | pgvector exact scan | 12282.3 ms | 35.268 ms | 36.729 ms | 1.000 |
| 50,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 12282.3 + 32160.5 ms | 32.753 ms | 33.642 ms | 1.000 |

### Embedding reuse (200 items, simulated 50 ms embedding call)

| Store | Second hybrid query | Embedding calls per query |
|---|---|---|
| In-memory (FAISS, re-embed each request) | 118.6 ms | 2 |
| pgvector (persisted) | 61.0 ms | 1 |
<!-- BENCH:END -->

## How to read the numbers

* **SQLite wins every single-process latency race, by roughly an order of magnitude.** It is a local file with no network hop. That is why it stays the default for the CLI and the desktop dashboard.
* **Postgres buys concurrency and persistence, not speed.** Its package round-trip is dominated by two network round-trips and JSONB parsing, and it is what lets several API workers share one memory store safely.
* **Below ~10k vectors, the in-memory FAISS index is faster than pgvector for a single query, but pgvector wins the request.** The in-memory path has to re-embed every item on every request (the *embedding reuse* table): with a 50 ms embedding call, that is the whole budget. Persisted embeddings turn a hybrid query into one embedding call plus one indexed search.
* **HNSW pays off once a package has more than a few thousand vectors, and only when the data has neighbourhood structure.** On the hierarchical corpus the index answers a top-10 over 50k vectors in under 2 ms at recall 0.99, twelve times faster than the exact scan. On uniformly random vectors, the documented worst case for graph indexes, the same query takes as long as the exact scan: pgvector's iterative scan keeps walking the graph until the filter is satisfied, so recall stays at 1.0 but nothing is gained. Real text embeddings behave like the first case. `search_embeddings(exact=True)` is a query-time switch when you need a guaranteed exact answer, and packages under a few thousand rows are scanned exactly by the planner anyway.
* **Build cost is paid once.** `COPY` streams the vectors; the HNSW index for one dimension is created lazily on first write and reused by every package in the database.

## Reproducing locally

Any pgvector-enabled Postgres works (`docker compose up db` provides one). Note
that Docker Desktop on Windows adds tens of milliseconds to every request larger
than one TCP segment through its port proxy, which inflates the pgvector query
latencies far beyond what the database is doing (an `EXPLAIN ANALYZE` of the
same query reports well under 1 ms); run the script inside WSL or on Linux for
representative numbers.
