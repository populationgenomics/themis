# syntax=docker/dockerfile:1

# Multi-stage build of the hello gRPC service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/hello/Dockerfile .`), so the committed
# themis/rpc stubs ship in the image. Dependencies come from the committed uv.lock via
# `uv sync --locked --group hello` — the same age-gated lock the rest of the repo uses.
# Cloud Run injects $PORT; __main__ binds [::]:$PORT and serves gRPC over HTTP/2.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# build: install the locked hello group into /app/.venv with a pinned uv. The root
# project is virtual (no [build-system]), so uv sync installs deps only.
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group hello --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# The image boots standalone on the fixture authorizer with an empty context map; the deploy
# overrides THEMIS_AUTHORIZER_BACKEND=http and supplies THEMIS_AUTH_URL (a missing value fails
# loud at startup).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    THEMIS_AUTHORIZER_BACKEND=fixture \
    THEMIS_HELLO_FIXTURE_CONTEXTS={}
COPY themis/rpc ./themis/rpc
COPY themis/clients ./themis/clients
COPY themis/services/hello ./themis/services/hello
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime,
# so no chown is needed.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.hello"]
