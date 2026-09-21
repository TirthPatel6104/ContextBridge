# ContextBridge benchmarks (2026-09-21)

Python 3.12.14 · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 4 CPUs · pgvector 0.8.6 · contextbridge 0.3.0

## Package storage (100-item package)

| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |
|---|---|---|---|---|
| package_roundtrip | 0.992 ms | 1.17 ms | 3.398 ms | 4.043 ms |
| listing_200_packages | 0.384 ms | 0.411 ms | 1.88 ms | 1.997 ms |

## Vector search (top-10, cosine, unit vectors)

Corpus: unit vectors around 64 random centres (mixture of Gaussians, spread 0.35), queries drawn the same way.

| n | dim | Backend | Build | Query median | Query p95 | Recall@10 |
|---|---|---|---|---|---|---|
| 1,000 | 384 | FAISS IndexFlatIP (in-memory) | 81.5 ms | 0.098 ms | 0.118 ms | 1.000 |
| 1,000 | 384 | numpy brute force (in-memory) | 0.1 ms | 0.067 ms | 0.082 ms | 1.000 |
| 1,000 | 384 | pgvector exact scan | 282.7 ms | 1.592 ms | 1.716 ms | 1.000 |
| 1,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 282.7 + 255.6 ms | 1.559 ms | 1.661 ms | 1.000 |
| 10,000 | 384 | FAISS IndexFlatIP (in-memory) | 347.5 ms | 0.638 ms | 0.669 ms | 1.000 |
| 10,000 | 384 | numpy brute force (in-memory) | 0.9 ms | 0.396 ms | 3.163 ms | 1.000 |
| 10,000 | 384 | pgvector exact scan | 22351.6 ms | 8.826 ms | 9.534 ms | 1.000 |
| 10,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 22351.6 + 1949.2 ms | 2.513 ms | 2.749 ms | 0.565 |
| 50,000 | 384 | FAISS IndexFlatIP (in-memory) | 1729.8 ms | 4.641 ms | 5.241 ms | 1.000 |
| 50,000 | 384 | numpy brute force (in-memory) | 10.8 ms | 3.817 ms | 7.754 ms | 1.000 |
| 50,000 | 384 | pgvector exact scan | 147666.0 ms | 27.778 ms | 28.157 ms | 1.000 |
| 50,000 | 384 | pgvector HNSW (m=16, ef=64/100) | 147666.0 + 33712.0 ms | 3.617 ms | 4.104 ms | 0.216 |

## Embedding reuse (200 items, simulated 50 ms embedding call)

| Store | Second hybrid query | Embedding calls per query |
|---|---|---|
| In-memory (FAISS, re-embed each request) | 117.1 ms | 2 |
| pgvector (persisted) | 61.5 ms | 1 |
