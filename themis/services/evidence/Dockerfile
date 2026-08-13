# syntax=docker/dockerfile:1

# Multi-stage build of the evidence gRPC service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/evidence/Dockerfile .`), so the committed
# themis/rpc stubs ship in the image. Dependencies come from the committed uv.lock via
# `uv sync --locked --group evidence`. Cloud Run injects $PORT; __main__ binds [::]:$PORT
# and serves gRPC over HTTP/2.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group evidence --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# No backend is baked in: each interface requires its own backend selector (and, for a
# fixture, its non-empty seed) and exits at startup without them. A baked fixture default would let a
# deploy that dropped the live override come up SERVING an empty corpus — every paper "not found", which
# reads as "genuinely absent" rather than a fault — so the fail-loud check must reach the deployed revision.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/rpc ./themis/rpc
# The litcache manifest stubs the literature live backend reads (+ the vendored buf/validate stubs
# litcache_pb2 imports); the litcache package init is docstring-only and the other litcache modules
# (conversion, fetch) are not imported by the read path.
COPY buf ./buf
COPY themis/litcache/__init__.py ./themis/litcache/__init__.py
COPY themis/litcache/models ./themis/litcache/models
COPY themis/services/evidence ./themis/services/evidence
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.evidence"]
