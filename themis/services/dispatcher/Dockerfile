# syntax=docker/dockerfile:1

# Multi-stage build of the dispatcher service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/dispatcher/Dockerfile .`). Dependencies come from the
# committed uv.lock via `uv sync --locked --group dispatcher` — the same age-gated lock the rest of
# the repo uses. Unlike the gRPC services this serves HTTP/1.1 (an HMAC webhook); Cloud Run injects
# $PORT and __main__ binds it. The image has no fixture mode — every required value comes from the
# deploy and a missing one fails loud at startup.
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
    uv sync --locked --group dispatcher --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/clients/work_queue ./themis/clients/work_queue
COPY themis/clients/auth ./themis/clients/auth
COPY themis/services/dispatcher ./themis/services/dispatcher
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime,
# so no chown is needed.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.dispatcher"]
