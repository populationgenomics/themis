# syntax=docker/dockerfile:1

# Multi-stage build of the cost exporter, the workspace-spend monitor's Cloud Run Job. Build context is
# the repo ROOT (deploy: `docker build -f themis/services/cost_exporter/Dockerfile .`). Dependencies come
# from the committed uv.lock via `uv sync --locked --group cost_exporter`. One execution is one run:
# __main__ reads its settings from the environment the Job injects, scans the workspace's sessions,
# sets and flushes the gauges, and exits; `report`, the morning report's entrypoint, runs from the same
# image as a second Job.
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
    uv sync --locked --group cost_exporter --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    MPLCONFIGDIR=/tmp/matplotlib
# The federation credential the exporter authenticates to the Claude API with; the rest of themis/clients
# is gRPC-side and has no consumer in this image. themis/telemetry is the meter pipeline the gauges ride.
COPY themis/clients/anthropic_wif.py ./themis/clients/anthropic_wif.py
COPY themis/telemetry ./themis/telemetry
COPY themis/services/cost_exporter ./themis/services/cost_exporter
# -x skips the venv's site-packages, which uv compiled at install.
RUN python -m compileall -q -x /site-packages/ /app
# Run unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
# Import both entrypoints at build time: a module the COPY set misses is otherwise only found when the
# scheduled execution fails, since nothing else imports the image's own tree. As the runtime user, so the
# matplotlib font cache the import seeds under MPLCONFIGDIR is the one that user reads at runtime.
RUN python -c 'import themis.services.cost_exporter.__main__, themis.services.cost_exporter.report'
CMD ["python", "-m", "themis.services.cost_exporter"]
