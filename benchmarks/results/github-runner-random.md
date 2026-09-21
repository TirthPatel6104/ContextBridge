# ContextBridge benchmarks (2026-09-21)

Python 3.12.14 · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 4 CPUs · pgvector 0.8.6 · contextbridge 0.4.0

## Package storage (100-item package)

| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |
|---|---|---|---|---|
| package_roundtrip | 1.036 ms | 1.323 ms | 3.629 ms | 4.228 ms |
| listing_200_packages | 0.397 ms | 0.412 ms | 2.175 ms | 2.711 ms |

## Vector search (top-10, cosine, random corpus)

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

## Embedding reuse (200 items, simulated 50 ms embedding call)

| Store | Second hybrid query | Embedding calls per query |
|---|---|---|
| In-memory (FAISS, re-embed each request) | 118.6 ms | 2 |
| pgvector (persisted) | 61.5 ms | 1 |
