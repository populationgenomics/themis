# syntax=docker/dockerfile:1

# Multi-stage build of the evidence convert worker. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/convert_worker/Dockerfile .`). Dependencies come
# from the committed uv.lock via `uv sync --locked --group convert_worker`. Cloud Run injects
# $PORT; __main__ binds [::]:$PORT and serves the pushed /convert task over HTTP.
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
    uv sync --locked --group convert_worker --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
# The worker runs the whole fetch/convert ladder, so it carries the full litcache package and the
# vendored buf/validate stubs litcache_pb2 imports — not the read service's lean models-only subset.
# themis/common is litcache's only cross-package themis import (writer/oa/crossref: storage + constants).
COPY buf ./buf
COPY themis/common ./themis/common
COPY themis/litcache ./themis/litcache
COPY themis/services/convert_worker ./themis/services/convert_worker
# Import the entrypoint at build time: a module the COPY set misses is otherwise only found when the
# deployed revision crash-loops, since nothing else imports the image's own tree.
RUN python -c 'import themis.services.convert_worker.__main__'
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.convert_worker"]
