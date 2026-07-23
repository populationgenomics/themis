# syntax=docker/dockerfile:1

# Multi-stage build of the auth gRPC service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/auth/Dockerfile .`), so the committed
# themis/rpc stubs ship in the image. Dependencies come from the committed uv.lock via
# `uv sync --locked --group auth` — the same age-gated lock the rest of the repo uses.
# Cloud Run injects $PORT; __main__ binds [::]:$PORT and serves gRPC over HTTP/2.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# build: install the locked auth group into /app/.venv with a pinned uv. The root
# project is virtual (no [build-system]), so uv sync installs deps only.
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group auth --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# No backend is baked in: the runtime requires THEMIS_BACKEND (and, for cloudsql, the
# THEMIS_SQL_* connection env) from whoever runs the image, and exits at startup without it.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/rpc ./themis/rpc
COPY themis/common/__init__.py themis/common/sql.py ./themis/common/
COPY themis/services/auth ./themis/services/auth
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime,
# so no chown is needed.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.auth"]
