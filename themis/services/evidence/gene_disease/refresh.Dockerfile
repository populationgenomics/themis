# syntax=docker/dockerfile:1

# Multi-stage build of the weekly gene-disease reference-refresh job. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/evidence/gene_disease/refresh.Dockerfile .`). Dependencies
# come from the committed uv.lock via `uv sync --locked --group evidence` — httpx drives the upstream
# fetches and google-cloud-storage the bucket writes (the serving deps ride along, unused by the job).
# The Cloud Run Job injects THEMIS_RESOURCES_BUCKET; a missing value fails loud at startup.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# build: install the locked evidence group into /app/.venv with a pinned uv. The root project is
# virtual (no [build-system]), so uv sync installs deps only.
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
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
# The job round-trips each fresh dump through the serving tree's own upstream parsers, which read the
# gene-disease validity classes from themis.svcv4 — those classes are keyed by the contract's enum,
# so the generated stubs are on the import path too.
COPY themis/rpc ./themis/rpc
COPY themis/evidence/models ./themis/evidence/models
COPY themis/svcv4 ./themis/svcv4
COPY themis/services/evidence ./themis/services/evidence
# Run unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime, no chown.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.evidence.gene_disease.refresh"]
