# ContextBridge benchmarks (2026-09-21)

Python 3.12.14 · Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 · 4 CPUs · pgvector 0.8.6 · contextbridge 0.4.0

## Package storage (100-item package)

| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |
|---|---|---|---|---|
| package_roundtrip | 1.159 ms | 1.633 ms | 3.635 ms | 4.421 ms |
| listing_200_packages | 0.385 ms | 0.418 ms | 2.023 ms | 2.111 ms |

## Vector search (top-10, cosine, hierarchical corpus)

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

## Embedding reuse (200 items, simulated 50 ms embedding call)

| Store | Second hybrid query | Embedding calls per query |
|---|---|---|
| In-memory (FAISS, re-embed each request) | 118.6 ms | 2 |
| pgvector (persisted) | 61.0 ms | 1 |
