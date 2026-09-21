# syntax=docker/dockerfile:1.7
#
# ContextBridge production image.
#
#   docker build -t contextbridge .
#   docker run -p 8000:8000 -e CB_DATABASE_URL=postgresql://... contextbridge
#
# Multi-stage: dependencies are built into a virtualenv in the builder stage
# so the runtime image carries no compilers.  The app runs as a non-root
# user, listens on 0.0.0.0:8000 (override with CB_HOST / CB_PORT), and stores
# SQLite data under /data when no CB_DATABASE_URL is given.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY contextbridge ./contextbridge
RUN pip install --upgrade pip \
    && pip install ".[api,postgres,otel,web]"

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="ContextBridge" \
      org.opencontainers.image.description="Portable, privacy-conscious working memory for LLM workflows" \
      org.opencontainers.image.source="https://github.com/TirthPatel6104/ContextBridge" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CB_HOST=0.0.0.0 \
    CB_PORT=8000 \
    CB_STORAGE_DIR=/data \
    CB_ENVIRONMENT=production \
    CB_LOG_LEVEL=INFO

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --create-home app \
    && mkdir -p /data \
    && chown -R app:app /data

COPY --from=builder /opt/venv /opt/venv

USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${CB_PORT}/readyz" || exit 1

CMD ["cb", "serve"]
