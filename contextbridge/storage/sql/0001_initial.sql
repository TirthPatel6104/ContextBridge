-- ContextBridge PostgreSQL schema, migration 0001.
-- Mirrors the SQLite layout: a summary row per package, every saved version
-- as a JSONB payload (identical to the portable format), and the egress ledger.

CREATE TABLE IF NOT EXISTS packages (
    name           TEXT PRIMARY KEY,
    version        INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    source_model   TEXT NOT NULL DEFAULT '',
    item_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS package_versions (
    name     TEXT NOT NULL REFERENCES packages(name) ON DELETE CASCADE,
    version  INTEGER NOT NULL,
    payload  JSONB NOT NULL,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (name, version)
);

CREATE INDEX IF NOT EXISTS idx_packages_updated ON packages (updated_at);

CREATE TABLE IF NOT EXISTS egress_log (
    id              BIGSERIAL PRIMARY KEY,
    package_name    TEXT NOT NULL,
    package_version INTEGER NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    target_model    TEXT NOT NULL,
    target_kind     TEXT NOT NULL,
    surface         TEXT NOT NULL DEFAULT '',
    query           TEXT NOT NULL DEFAULT '',
    item_ids        JSONB NOT NULL DEFAULT '[]'::jsonb,
    item_count      INTEGER NOT NULL DEFAULT 0,
    withheld_count  INTEGER NOT NULL DEFAULT 0,
    tokens          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_egress_package ON egress_log (package_name, ts DESC);
