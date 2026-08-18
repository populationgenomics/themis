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
# The read path reaches into litcache for the manifest stubs, the readiness derivation, the manifest
# key, and the crosswalk lookup, and litcache reaches themis/common for storage + the Cloud SQL dial —
# so both packages ship whole rather than as a hand-listed subset that silently rots as the read path
# grows. buf carries the vendored validate stubs litcache_pb2 imports.
COPY buf ./buf
COPY themis/common ./themis/common
COPY themis/litcache ./themis/litcache
COPY themis/services/evidence ./themis/services/evidence
# Import the entrypoint at build time: a module the COPY set misses is otherwise only found when the
# deployed revision crash-loops, since nothing else imports the image's own tree.
RUN python -c 'import themis.services.evidence.__main__'
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.evidence"]
