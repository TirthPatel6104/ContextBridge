# Deployment

ContextBridge is local-first: the default install is a SQLite file and a
server bound to loopback. This document covers the other end of the spectrum,
running it as a shared service with PostgreSQL + pgvector, in Docker, and on
AWS, with the API guarded by a key and traces exported to a collector.

## Runtime surfaces

| Entry point | What it is | When to use it |
|---|---|---|
| `cb serve` / `uvicorn contextbridge.api.app:app_factory --factory` | **FastAPI** server: dashboard at `/`, JSON API at `/api/v1` (OpenAPI at `/docs`), the unversioned `/api` alias the extension uses, `/livez` and `/readyz` probes | Everything from local use to production |
| `python -m contextbridge.web.app` | The original Flask dashboard | Kept for compatibility; no auth, no probes, no tracing |
| `cb …` | CLI | Scripts and terminals, same service layer |

The FastAPI app is created by `contextbridge.api.create_app(settings, store=…, service=…)`, so tests inject a temporary store and the production entry point reads `Settings.from_env()`.

## Configuration

Everything is an environment variable (see `.env.example`). The ones that matter beyond a laptop:

| Variable | Effect |
|---|---|
| `CB_DATABASE_URL` | PostgreSQL DSN. Setting it selects the `postgres` backend unless `CB_STORAGE_BACKEND` says otherwise. Schema migrations run on start-up. |
| `CB_API_KEY` | When set, every `/api` route requires `X-API-Key: <key>` (or `Authorization: Bearer <key>`). Probes stay open. Without it the API is open, so only bind to loopback. |
| `CB_HOST`, `CB_PORT` | Bind address. The container sets `0.0.0.0:8000`. |
| `CB_ALLOWED_ORIGINS` | Browser origins allowed by CORS (the chat sites the extension runs on). |
| `CB_ENVIRONMENT` | `development` / `staging` / `production`; reported in `/api/v1/health` and on every trace. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP collector, e.g. `http://otel-collector:4318`. Enables tracing. See [OBSERVABILITY.md](OBSERVABILITY.md). |

The dashboard sends the API key from `localStorage["cb_api_key"]`; when a request comes back `401` it shows an inline field to enter it.

## PostgreSQL + pgvector

`contextbridge[postgres]` adds `psycopg[binary,pool]` and `pgvector`. The store keeps the SQLite layout (`packages`, `package_versions` as JSONB, `egress_log`) and adds `item_embeddings`, one vector per *(package, embedding model, item)*, with an HNSW index per vector dimension.

```bash
# 1. a database with the extension available (RDS, Cloud SQL, Neon, Supabase, or docker compose up db)
export CB_DATABASE_URL=postgresql://user:pass@host:5432/contextbridge

# 2. schema
cb db status          # applied vs. latest migration, pgvector availability
cb db upgrade         # apply pending migrations (also happens automatically on start)

# 3. move your local memory across (all versions + egress ledger; SQLite is untouched)
cb migrate --to postgres [--only NAME]… [--overwrite]

# 4. run
cb serve
```

Migrations are numbered SQL files in `contextbridge/storage/sql/`, applied once each and recorded in `schema_migrations`. They are additive only; the payload format is versioned separately by `SCHEMA_VERSION`.

Why pgvector rather than FAISS on the server: the in-memory `VectorStore` re-embeds a package on every request and holds vectors for one process. With Postgres, embeddings persist (`known_hashes()` lets the retriever embed only new or changed items), every worker shares them, and HNSW answers top-k over corpora far larger than a per-request index. The measured trade-offs are in [BENCHMARKS.md](BENCHMARKS.md).

## Docker

```bash
docker build -t contextbridge .
docker run --rm -p 8000:8000 -v cb-data:/data contextbridge            # SQLite in the volume
docker run --rm -p 8000:8000 -e CB_DATABASE_URL=postgresql://… contextbridge
```

The image is a two-stage build on `python:3.12-slim`: dependencies are compiled into a virtualenv in the builder stage, the runtime stage has no compilers, runs as the non-root `app` user, exposes `8000`, stores SQLite data under `/data`, and has a `HEALTHCHECK` on `/readyz`.

`docker compose up --build` starts the app against a `pgvector/pgvector:pg16` database; `--profile observability` adds an OpenTelemetry collector and Jaeger (traces at http://localhost:16686). CI runs the same compose file plus `docker-compose.ci.yml` and executes `scripts/e2e.py` against it.

## AWS

`infra/aws/` is a Terraform module that provisions:

* an **ECR** repository (scan on push, keep the last 20 images);
* **RDS PostgreSQL 16** (`db.t4g.micro`, 20 GB gp3, encrypted, 7-day backups, not publicly accessible) — pgvector is a bundled RDS extension and the app's first migration enables it;
* an **App Runner** service running the image with 1 vCPU / 2 GB, autoscaling 1–3 instances, health-checked on `/readyz`, reaching the database through a VPC connector, with `CB_DATABASE_URL` and `CB_API_KEY` injected from **Secrets Manager**;
* an **IAM role for GitHub Actions** assumable through OIDC (no long-lived keys), limited to pushing to that repository and updating that service.

```bash
cd infra/aws
terraform init
terraform apply -var="github_repo=<owner>/<repo>"          # ~10 minutes, mostly RDS
terraform output                                            # service_url, deploy_role_arn, …
aws secretsmanager get-secret-value --secret-id "$(terraform output -raw api_key_secret_arn)" --query SecretString --output text
```

Then set repository **variables** `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY`, `APP_RUNNER_SERVICE_ARN` and the **secret** `CB_API_KEY` in GitHub, and push to `main` (or run *Deploy to AWS* manually). The workflow builds the image, pushes it as `:<sha>` and `:latest`, calls `apprunner update-service`, waits for `RUNNING`, and runs `scripts/e2e.py` against the public URL. First deployment note: App Runner needs an image to exist at `image_tag` (default `latest`) when the service is created, so either push once by hand (`docker build`, `aws ecr get-login-password | docker login`, `docker push`) before `terraform apply`, or apply with `-target=aws_ecr_repository.app` first, push, then apply the rest.

Approximate cost at 2026 list prices: about $60–70 per month idle (App Runner provisioned instance + RDS micro); pausing the App Runner service leaves only the database.

Tracing on AWS: run the AWS Distro for OpenTelemetry collector (as a sidecar on ECS or a small service) with the X-Ray exporter and set `otel_endpoint` to it; App Runner cannot run sidecars itself, so the collector must be reachable through the VPC connector.

### Alternatives

The same image runs unchanged on ECS Fargate behind an ALB (use the Terraform module as a starting point and replace the App Runner resources), on Fly.io / Railway / Render (set `CB_DATABASE_URL`, `CB_API_KEY`, `CB_HOST=0.0.0.0`), or on any Kubernetes cluster (readiness probe `/readyz`, liveness `/livez`).

## Operations checklist

* **Back-ups**: RDS automated backups cover packages, ledger and embeddings. For SQLite, copy `contextbridge.db` (WAL mode; use `sqlite3 .backup` or stop the server).
* **Rotation**: change `CB_API_KEY` in Secrets Manager and redeploy; the key is compared with `secrets.compare_digest` and never logged.
* **Upgrades**: schema migrations run on start-up; `cb db status` shows what is pending. Payload upgrades (new item fields) happen lazily on load, as before.
* **Logs** never contain memory content, prompts, or keys — only counts and package names.
