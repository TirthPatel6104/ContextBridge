"""
Storage and vector-search benchmarks: SQLite vs PostgreSQL, FAISS / numpy vs pgvector.

Run against a pgvector-enabled database::

    python benchmarks/bench.py --database-url postgresql://cb:cb@localhost:5432/cb \
        --sizes 1000,10000,50000 --dimension 384 \
        --output benchmarks/results/local.json --markdown benchmarks/results/local.md

What is measured
----------------
package_roundtrip
    Save + load of a package with 100 items, SQLite vs Postgres (median of N).
listing
    ``summaries()`` with 200 packages present.
vector_index_build
    Time to insert *n* vectors of *dimension* into FAISS (IndexFlatIP), the
    numpy fallback, and pgvector (bulk upsert + HNSW build).
vector_query
    Median / p95 latency of a top-10 query, plus **recall@10 of HNSW against
    exact search** on 200 random queries (pgvector's approximate index vs.
    brute force over the same rows).  FAISS flat and numpy are exact, so their
    recall is 1.0 by construction.
embedding_reuse
    The end-to-end cost of a hybrid retrieval when embeddings are already
    persisted (pgvector) versus re-embedded per request (in-memory store),
    using a deterministic hash embedder so no API is involved.

Numbers are wall-clock on the machine that runs the script; the JSON output
records Python, platform, CPU count and the pgvector version so a published
table can name its hardware.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextbridge import __version__  # noqa: E402
from contextbridge.models import (  # noqa: E402
    ContextPackage,
    MemoryCategory,
    MemoryItem,
    StructuredMemory,
)
from contextbridge.storage.sqlite_store import SQLiteStore  # noqa: E402
from contextbridge.storage.vector_store import VectorStore  # noqa: E402

RNG = np.random.default_rng(42)


def _timeit(fn, *, repeat: int) -> dict[str, float]:
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
        "min_ms": round(samples[0], 3),
        "runs": repeat,
    }


def _package(name: str, items: int = 100) -> ContextPackage:
    cats = list(MemoryCategory)
    memory = StructuredMemory.from_items(
        [
            MemoryItem(
                category=cats[i % len(cats)],
                content=f"Benchmark statement number {i} about topic {i % 17} and detail {i * 7}",
                source=f"rule:bench_{i}",
                origin="bench",
            )
            for i in range(items)
        ]
    )
    return ContextPackage(name=name, memory=memory)


# ---------------------------------------------------------------------------
# Storage benchmarks
# ---------------------------------------------------------------------------


def bench_storage(sqlite_dir: Path, pg: Any | None, *, repeat: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    sqlite = SQLiteStore(base_dir=sqlite_dir)
    pkg = _package("bench_pkg")

    def sqlite_roundtrip():
        sqlite.save(pkg)
        sqlite.load("bench_pkg")

    results["package_roundtrip"] = {"sqlite": _timeit(sqlite_roundtrip, repeat=repeat)}
    for i in range(200):
        sqlite.save(_package(f"list_{i:03d}", items=10))
    results["listing_200_packages"] = {"sqlite": _timeit(sqlite.summaries, repeat=repeat)}
    sqlite.close()

    if pg is not None:

        def pg_roundtrip():
            pg.save(pkg)
            pg.load("bench_pkg")

        results["package_roundtrip"]["postgres"] = _timeit(pg_roundtrip, repeat=repeat)
        for i in range(200):
            pg.save(_package(f"list_{i:03d}", items=10))
        results["listing_200_packages"]["postgres"] = _timeit(pg.summaries, repeat=repeat)
    return results


# ---------------------------------------------------------------------------
# Vector benchmarks
# ---------------------------------------------------------------------------


def _unit_vectors(n: int, dim: int) -> np.ndarray:
    v = RNG.standard_normal((n, dim)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def _clustered_vectors(n: int, dim: int, *, clusters: int = 64, spread: float = 0.35) -> np.ndarray:
    """Unit vectors drawn around *clusters* random centres.

    Uniformly random high-dimensional vectors are the worst case for graph
    indexes (every point is almost equidistant from every other), so they
    understate HNSW recall badly.  Real embeddings live on low-dimensional
    manifolds with topic structure; a mixture of Gaussians is the standard
    stand-in.  Query vectors are drawn the same way.
    """
    centres = RNG.standard_normal((clusters, dim)).astype(np.float32)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    assign = RNG.integers(0, clusters, size=n)
    v = centres[assign] + spread * RNG.standard_normal((n, dim)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def _exact_topk(matrix: np.ndarray, queries: np.ndarray, k: int) -> list[list[int]]:
    sims = queries @ matrix.T
    return [list(np.argsort(-row)[:k]) for row in sims]


def bench_vectors(
    pg: Any | None, *, n: int, dim: int, queries: int, top_k: int, repeat: int
) -> dict[str, Any]:
    vectors = _clustered_vectors(n, dim)
    ids = [f"item{i:07d}" for i in range(n)]
    qs = _clustered_vectors(queries, dim)
    truth = _exact_topk(vectors, qs, top_k)
    out: dict[str, Any] = {"n": n, "dimension": dim, "queries": queries, "top_k": top_k}

    # FAISS flat (exact inner product)
    faiss_store = VectorStore(dimension=dim)
    t0 = time.perf_counter()
    faiss_store.add(ids, vectors.tolist())
    out["faiss_flat"] = {
        "backend": "faiss" if faiss_store._index is not None else "numpy",
        "build_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    qi = iter(range(10**9))

    def faiss_query():
        faiss_store.search(qs[next(qi) % queries].tolist(), top_k=top_k)

    out["faiss_flat"]["query"] = _timeit(faiss_query, repeat=repeat)
    out["faiss_flat"]["recall_at_k"] = 1.0

    # numpy brute force (the fallback path when faiss is absent)
    t0 = time.perf_counter()
    matrix = vectors.copy()
    out["numpy_bruteforce"] = {"build_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def numpy_query():
        q = qs[next(qi) % queries]
        np.argsort(-(matrix @ q))[:top_k]

    out["numpy_bruteforce"]["query"] = _timeit(numpy_query, repeat=repeat)
    out["numpy_bruteforce"]["recall_at_k"] = 1.0

    if pg is None:
        return out

    # pgvector: bulk upsert, then exact vs HNSW
    name = "bench_vec"
    pg.save(ContextPackage(name=name))
    pg.delete_embeddings(name)
    rows = [(ids[i], "h", vectors[i].tolist()) for i in range(n)]
    t0 = time.perf_counter()
    batch = 2000
    for start in range(0, n, batch):
        pg.upsert_embeddings(name, rows[start : start + batch], model="bench", index=False)
    insert_ms = (time.perf_counter() - t0) * 1000
    pg.drop_hnsw_index(dim)

    def pg_exact():
        pg.search_embeddings(
            name, qs[next(qi) % queries].tolist(), top_k=top_k, model="bench", exact=True
        )

    out["pgvector_exact"] = {
        "insert_ms": round(insert_ms, 1),
        "query": _timeit(pg_exact, repeat=repeat),
    }
    out["pgvector_exact"]["recall_at_k"] = 1.0

    t0 = time.perf_counter()
    pg.ensure_hnsw_index(dim)
    build_ms = (time.perf_counter() - t0) * 1000

    def pg_hnsw():
        pg.search_embeddings(name, qs[next(qi) % queries].tolist(), top_k=top_k, model="bench")

    hits = 0
    for i in range(queries):
        got = pg.search_embeddings(name, qs[i].tolist(), top_k=top_k, model="bench")
        got_idx = {int(g[0][4:]) for g in got}
        hits += len(got_idx & set(int(t) for t in truth[i]))
    out["pgvector_hnsw"] = {
        "insert_ms": round(insert_ms, 1),
        "index_build_ms": round(build_ms, 1),
        "query": _timeit(pg_hnsw, repeat=repeat),
        "recall_at_k": round(hits / (queries * top_k), 4),
    }
    pg.delete_embeddings(name)
    pg.delete(name)
    return out


# ---------------------------------------------------------------------------
# Embedding reuse (end-to-end hybrid retrieval)
# ---------------------------------------------------------------------------


class _HashEmbedder:
    """Deterministic, API-free embedder with a fixed per-call cost (simulates a provider)."""

    semantic_embeddings = True
    name = "bench-embedder"

    def __init__(self, dim: int, latency_ms: float) -> None:
        self.dim = dim
        self.latency = latency_ms / 1000
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        time.sleep(self.latency)
        seed = abs(hash(text)) % (2**32)
        v = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        time.sleep(self.latency)
        return [await self._one(t) for t in texts]

    async def _one(self, text: str) -> list[float]:
        seed = abs(hash(text)) % (2**32)
        v = np.random.default_rng(seed).standard_normal(self.dim).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()


def bench_embedding_reuse(
    pg: Any | None, sqlite_dir: Path, *, items: int, dim: int
) -> dict[str, Any]:
    from contextbridge.config import Settings
    from contextbridge.models import RetrievalOptions
    from contextbridge.service import ContextBridgeService

    out: dict[str, Any] = {"items": items, "dimension": dim, "simulated_embed_latency_ms": 50}
    pkg = _package("reuse", items=items)

    sqlite = SQLiteStore(base_dir=sqlite_dir / "reuse")
    sqlite.save(pkg)
    svc = ContextBridgeService(sqlite, settings=Settings(storage_dir=sqlite_dir))
    emb = _HashEmbedder(dim, 50)
    svc.retrieve("reuse", "topic 3 detail", RetrievalOptions(top_k=5), adapter=emb)
    t0 = time.perf_counter()
    svc.retrieve("reuse", "topic 5 detail", RetrievalOptions(top_k=5), adapter=emb)
    out["in_memory_store_second_query_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["in_memory_store_embed_calls_per_query"] = 2  # batch for items + one for the query
    sqlite.close()

    if pg is not None:
        pg.save(pkg)
        pg.delete_embeddings("reuse")
        svc = ContextBridgeService(pg, settings=Settings(storage_dir=sqlite_dir))
        emb = _HashEmbedder(dim, 50)
        svc.retrieve("reuse", "topic 3 detail", RetrievalOptions(top_k=5), adapter=emb)
        calls_before = emb.calls
        t0 = time.perf_counter()
        svc.retrieve("reuse", "topic 5 detail", RetrievalOptions(top_k=5), adapter=emb)
        out["pgvector_second_query_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["pgvector_embed_calls_per_query"] = emb.calls - calls_before
        pg.delete("reuse")
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def to_markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    lines = [
        f"# ContextBridge benchmarks ({report['generated_at'][:10]})",
        "",
        f"Python {env['python']} · {env['platform']} · {env['cpu_count']} CPUs · "
        f"pgvector {env.get('pgvector') or 'n/a'} · contextbridge {env['contextbridge']}",
        "",
        "## Package storage (100-item package)",
        "",
        "| Operation | SQLite median | SQLite p95 | Postgres median | Postgres p95 |",
        "|---|---|---|---|---|",
    ]
    for op, backends in report["storage"].items():
        s = backends.get("sqlite", {})
        p = backends.get("postgres", {})
        lines.append(
            f"| {op} | {s.get('median_ms', '–')} ms | {s.get('p95_ms', '–')} ms | "
            f"{p.get('median_ms', '–')} ms | {p.get('p95_ms', '–')} ms |"
        )
    lines += [
        "",
        "## Vector search (top-10, cosine, unit vectors)",
        "",
        "Corpus: unit vectors around 64 random centres (mixture of Gaussians, "
        "spread 0.35), queries drawn the same way.",
        "",
        "| n | dim | Backend | Build | Query median | Query p95 | Recall@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    for run in report["vectors"]:
        n, dim = run["n"], run["dimension"]
        for key, label in (
            ("faiss_flat", "FAISS IndexFlatIP (in-memory)"),
            ("numpy_bruteforce", "numpy brute force (in-memory)"),
            ("pgvector_exact", "pgvector exact scan"),
            ("pgvector_hnsw", "pgvector HNSW (m=16, ef=64/100)"),
        ):
            r = run.get(key)
            if not r:
                continue
            build = r.get("index_build_ms", r.get("build_ms", r.get("insert_ms", 0)))
            if key == "pgvector_hnsw":
                build = f"{r['insert_ms']} + {r['index_build_ms']}"
            lines.append(
                f"| {n:,} | {dim} | {label} | {build} ms | {r['query']['median_ms']} ms | "
                f"{r['query']['p95_ms']} ms | {r['recall_at_k']:.3f} |"
            )
    reuse = report.get("embedding_reuse") or {}
    if reuse:
        latency = reuse["simulated_embed_latency_ms"]
        mem_ms = reuse["in_memory_store_second_query_ms"]
        mem_calls = reuse["in_memory_store_embed_calls_per_query"]
        lines += [
            "",
            f"## Embedding reuse ({reuse['items']} items, simulated {latency} ms embedding call)",
            "",
            "| Store | Second hybrid query | Embedding calls per query |",
            "|---|---|---|",
            f"| In-memory (FAISS, re-embed each request) | {mem_ms} ms | {mem_calls} |",
        ]
        if "pgvector_second_query_ms" in reuse:
            lines.append(
                f"| pgvector (persisted) | {reuse['pgvector_second_query_ms']} ms | "
                f"{reuse['pgvector_embed_calls_per_query']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--database-url", default=os.getenv("CB_BENCH_DATABASE_URL", ""))
    ap.add_argument("--sizes", default="1000,10000")
    ap.add_argument("--dimension", type=int, default=384)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--repeat", type=int, default=50)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--markdown", type=Path, default=None)
    args = ap.parse_args()

    import tempfile

    pg = None
    schema = None
    if args.database_url:
        import psycopg

        from contextbridge.storage.postgres_store import PostgresStore

        schema = "bench_" + uuid.uuid4().hex[:8]
        with psycopg.connect(args.database_url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(f"CREATE SCHEMA {schema}")
        sep = "&" if "?" in args.database_url else "?"
        pg = PostgresStore(f"{args.database_url}{sep}options=-c%20search_path%3D{schema}%2Cpublic")

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "contextbridge": __version__,
            "pgvector": pg.stats().get("pgvector") if pg else None,
        },
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            print("storage…", flush=True)
            report["storage"] = bench_storage(tmp_path, pg, repeat=args.repeat)
            report["vectors"] = []
            for n in [int(s) for s in args.sizes.split(",") if s.strip()]:
                print(f"vectors n={n}…", flush=True)
                report["vectors"].append(
                    bench_vectors(
                        pg,
                        n=n,
                        dim=args.dimension,
                        queries=args.queries,
                        top_k=10,
                        repeat=args.repeat,
                    )
                )
            print("embedding reuse…", flush=True)
            report["embedding_reuse"] = bench_embedding_reuse(
                pg, tmp_path, items=200, dim=args.dimension
            )
    finally:
        if pg is not None:
            pg.close()
            import psycopg

            with psycopg.connect(args.database_url, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")

    md = to_markdown(report)
    print(md)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
