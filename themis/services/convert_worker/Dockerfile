# syntax=docker/dockerfile:1

# Multi-stage build of the evidence convert worker. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/convert_worker/Dockerfile .`). Dependencies come
# from the committed uv.lock via `uv sync --locked --group convert_worker`. Cloud Run injects
# $PORT; __main__ binds [::]:$PORT and serves the pushed /convert task over HTTP.
# A named stage rather than `COPY --from=<image>`: Dependabot updates images on FROM lines only.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The base image ships the standard library without bytecode, and PYTHONDONTWRITEBYTECODE stops a process caching
# its own; each image compiles its /app tree the same way after its last COPY.
RUN python -m compileall -q /usr/local/lib/python3.13

FROM base AS build
COPY --from=uv /uv /uvx /bin/
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
# themis/common is litcache's only cross-package themis import (writer/oa/crossref: storage + constants);
# anthropic_wif is the federation credential the PDF branch authenticates to the Claude API with — the
# rest of themis/clients is gRPC-side and has no consumer here. themis/telemetry carries the
# transcription's token counts to Cloud Monitoring.
COPY buf ./buf
COPY themis/clients/anthropic_wif.py ./themis/clients/anthropic_wif.py
COPY themis/common ./themis/common
COPY themis/litcache ./themis/litcache
COPY themis/telemetry ./themis/telemetry
COPY themis/services/convert_worker ./themis/services/convert_worker
# -x skips the venv's site-packages, which uv compiled at install.
RUN python -m compileall -q -x /site-packages/ /app
# Import the entrypoint at build time: a module the COPY set misses is otherwise only found when the
# deployed revision crash-loops, since nothing else imports the image's own tree.
RUN python -c 'import themis.services.convert_worker.__main__'
# The sweep's job runs this image too.
RUN python -c 'import themis.litcache.sweep'
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.convert_worker"]
