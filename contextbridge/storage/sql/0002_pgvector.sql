-- ContextBridge PostgreSQL schema, migration 0002: pgvector item embeddings.
--
-- One row per (package, item, embedding model).  Vectors are stored with an
-- explicit dimension per model so that OpenAI (1536), Ollama (768/1024) and
-- small local models (384) can coexist.  The HNSW index gives approximate
-- nearest-neighbour search with cosine distance; exact search is used when
-- the index is absent.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS item_embeddings (
    package_name  TEXT NOT NULL REFERENCES packages(name) ON DELETE CASCADE,
    item_id       TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT 'default',
    dimension     INTEGER NOT NULL,
    embedding     VECTOR NOT NULL,
    content_hash  TEXT NOT NULL DEFAULT '',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (package_name, model, item_id)
);

CREATE INDEX IF NOT EXISTS idx_item_embeddings_package ON item_embeddings (package_name, model);
