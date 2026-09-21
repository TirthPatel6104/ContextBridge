# ContextBridge benchmarks (2026-09-21)

Python 3.12.14 · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 4 CPUs · pgvector 0.8.6 · contextbridge 0.4.0

## Package storage (100-item package)

| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |
|---|---|---|---|---|
| package_roundtrip | 1.024 ms | 1.374 ms | 3.625 ms | 4.823 ms |
| listing_200_packages | 0.388 ms | 0.419 ms | 2.086 ms | 2.208 ms |

## Vector search (top-10, cosine, unit vectors)

Corpus: unit vectors around 64 random centres (mixture of Gaussians, relative spread 0.6), queries drawn the same way.

| n | dim | Backend | Build | Query median | Query p95 | Recall@10 |
|---|---|---|---|---|---|---|
| 1,000 | 384 | FAISS IndexFlatIP (in-memory) | 78.0 ms | 0.097 ms | 0.122 ms | 1.000 |
| 1,000 | 384 | numpy brute force (in-memory) | 0.1 ms | 0.071 ms | 0.088 ms | 1.000 |
| 1,000 | 384 | pgvector exact scan | 247.7 ms | 1.744 ms | 1.838 ms | 1.000 |
| 1,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 247.7 + 194.9 ms | 1.736 ms | 1.782 ms | 1.000 |
| 10,000 | 384 | FAISS IndexFlatIP (in-memory) | 310.2 ms | 0.667 ms | 1.127 ms | 1.000 |
| 10,000 | 384 | numpy brute force (in-memory) | 1.2 ms | 0.483 ms | 1.665 ms | 1.000 |
| 10,000 | 384 | pgvector exact scan | 2512.6 ms | 6.034 ms | 7.832 ms | 1.000 |
| 10,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 2512.6 + 958.1 ms | 1.766 ms | 1.975 ms | 0.703 |
| 50,000 | 384 | FAISS IndexFlatIP (in-memory) | 1511.1 ms | 4.759 ms | 5.276 ms | 1.000 |
| 50,000 | 384 | numpy brute force (in-memory) | 9.2 ms | 3.402 ms | 3.698 ms | 1.000 |
| 50,000 | 384 | pgvector exact scan | 12868.8 ms | 22.649 ms | 27.106 ms | 1.000 |
| 50,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 12868.8 + 15402.7 ms | 1.936 ms | 2.789 ms | 0.365 |

## Embedding reuse (200 items, simulated 50 ms embedding call)

| Store | Second hybrid query | Embedding calls per query |
|---|---|---|
| In-memory (FAISS, re-embed each request) | 117.2 ms | 2 |
| pgvector (persisted) | 61.0 ms | 1 |
