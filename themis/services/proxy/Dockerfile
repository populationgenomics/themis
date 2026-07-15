# syntax=docker/dockerfile:1

# Multi-stage build of the credential-proxy sidecar. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/proxy/Dockerfile .`). Dependencies come from the
# committed uv.lock via `uv sync --locked --group proxy`. The proxy holds the two credentials (the
# environment key and the session token) that must never reach the agent container; it has no fixture
# mode — every value comes from the dispatcher's per-execution env, and a missing one fails loud.
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
    uv sync --locked --group proxy --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/clients/id_token.py ./themis/clients/id_token.py
COPY themis/services/proxy ./themis/services/proxy
# Serve unprivileged (defense-in-depth); the injected credentials live only in this container's env,
# unreadable from the agent container (separate PID/mount namespaces).
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.proxy"]
