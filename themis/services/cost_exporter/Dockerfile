# syntax=docker/dockerfile:1

# Multi-stage build of the cost exporter, the workspace-spend monitor's Cloud Run Job. Build context is
# the repo ROOT (deploy: `docker build -f themis/services/cost_exporter/Dockerfile .`). Dependencies come
# from the committed uv.lock via `uv sync --locked --group cost_exporter`. One execution is one run:
# __main__ reads its settings from the environment the Job injects, scans the workspace's sessions,
# sets and flushes the gauges, and exits.
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
    uv sync --locked --group cost_exporter --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
# The federation credential the exporter authenticates to the Claude API with; the rest of themis/clients
# is gRPC-side and has no consumer in this image. themis/telemetry is the meter pipeline the gauges ride.
COPY themis/clients/anthropic_wif.py ./themis/clients/anthropic_wif.py
COPY themis/telemetry ./themis/telemetry
COPY themis/services/cost_exporter ./themis/services/cost_exporter
# Import the entrypoint at build time: a module the COPY set misses is otherwise only found when the
# scheduled execution fails, since nothing else imports the image's own tree.
RUN python -c 'import themis.services.cost_exporter.__main__'
# Run unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.cost_exporter"]
