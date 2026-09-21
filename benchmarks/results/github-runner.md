# ContextBridge benchmarks (2026-09-21)

Python 3.12.14 · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 4 CPUs · pgvector 0.8.6 · contextbridge 0.4.0

## Package storage (100-item package)

| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |
|---|---|---|---|---|
| package_roundtrip | 0.966 ms | 1.115 ms | 3.265 ms | 3.882 ms |
| listing_200_packages | 0.39 ms | 0.403 ms | 1.933 ms | 2.56 ms |

## Vector search (top-10, cosine, unit vectors)

Corpus: unit vectors around 64 random centres (mixture of Gaussians, relative spread 0.6), queries drawn the same way.

| n | dim | Backend | Build | Query median | Query p95 | Recall@10 |
|---|---|---|---|---|---|---|
| 1,000 | 384 | FAISS IndexFlatIP (in-memory) | 79.7 ms | 0.099 ms | 0.119 ms | 1.000 |
| 1,000 | 384 | numpy brute force (in-memory) | 0.1 ms | 0.067 ms | 0.081 ms | 1.000 |
| 1,000 | 384 | pgvector exact scan | 265.6 ms | 1.547 ms | 1.671 ms | 1.000 |
| 1,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 265.6 + 197.5 ms | 1.568 ms | 1.613 ms | 1.000 |
| 10,000 | 384 | FAISS IndexFlatIP (in-memory) | 340.9 ms | 0.633 ms | 0.662 ms | 1.000 |
| 10,000 | 384 | numpy brute force (in-memory) | 0.9 ms | 0.397 ms | 3.212 ms | 1.000 |
| 10,000 | 384 | pgvector exact scan | 2625.8 ms | 7.383 ms | 9.431 ms | 1.000 |
| 10,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 2625.8 + 982.6 ms | 6.962 ms | 7.288 ms | 1.000 |
| 50,000 | 384 | FAISS IndexFlatIP (in-memory) | 1684.8 ms | 4.431 ms | 4.484 ms | 1.000 |
| 50,000 | 384 | numpy brute force (in-memory) | 4.8 ms | 3.667 ms | 5.486 ms | 1.000 |
| 50,000 | 384 | pgvector exact scan | 13718.1 ms | 38.494 ms | 39.912 ms | 1.000 |
| 50,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 13718.1 + 16004.4 ms | 35.806 ms | 37.527 ms | 1.000 |

## Embedding reuse (200 items, simulated 50 ms embedding call)

| Store | Second hybrid query | Embedding calls per query |
|---|---|---|
| In-memory (FAISS, re-embed each request) | 116.5 ms | 2 |
| pgvector (persisted) | 60.7 ms | 1 |
