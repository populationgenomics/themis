# syntax=docker/dockerfile:1

# Multi-stage build of the sheaf gRPC service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/sheaf/Dockerfile .`), so the committed
# themis/rpc stubs ship in the image. Dependencies come from the committed uv.lock via
# `uv sync --locked --group sheaf_service` — the same age-gated lock the rest of the repo uses.
# Cloud Run injects $PORT; __main__ binds [::]:$PORT and serves gRPC over HTTP/2.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# build: install the locked sheaf_service group into /app/.venv with a pinned uv. The root
# project is virtual (no [build-system]), so uv sync installs deps only.
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group sheaf_service --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Nothing is baked in: the runtime requires the authorizer selector, the backend selector and
# the three ceilings from whoever runs the image, and exits at startup without them.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/rpc ./themis/rpc
COPY themis/clients ./themis/clients
# The sheaf library whole: the store, the ref document's stubs under models/, and both backends.
COPY themis/sheaf ./themis/sheaf
COPY themis/services/sheaf ./themis/services/sheaf
# Import the entrypoint at build time, so a module the COPY set misses fails the build.
RUN python -c 'import themis.services.sheaf.__main__'
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime,
# so no chown is needed.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.sheaf"]
