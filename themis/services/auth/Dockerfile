# syntax=docker/dockerfile:1

# Multi-stage build of the auth gRPC service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/auth/Dockerfile .`), so the committed
# themis/rpc stubs ship in the image. Dependencies come from the committed uv.lock via
# `uv sync --locked --group auth` — the same age-gated lock the rest of the repo uses.
# Cloud Run injects $PORT; __main__ binds [::]:$PORT and serves gRPC over HTTP/2.
# A named stage rather than `COPY --from=<image>`: Dependabot updates images on FROM lines only.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The base image ships the standard library without bytecode, and PYTHONDONTWRITEBYTECODE stops a process caching
# its own; each image compiles its /app tree the same way after its last COPY.
RUN python -m compileall -q /usr/local/lib/python3.13

# build: install the locked auth group into /app/.venv with a pinned uv. The root
# project is virtual (no [build-system]), so uv sync installs deps only.
FROM base AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group auth --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# No backend is baked in: the runtime requires THEMIS_BACKEND, the ACL's secret and staleness
# limit, and the authorizer selector from whoever runs the image, and exits at startup without them.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/rpc ./themis/rpc
COPY themis/clients ./themis/clients
COPY themis/common/__init__.py themis/common/sql.py themis/common/sql_pool.py ./themis/common/
COPY themis/telemetry ./themis/telemetry
COPY themis/services/auth ./themis/services/auth
# -x skips the venv's site-packages, which uv compiled at install.
RUN python -m compileall -q -x /site-packages/ /app
# Import the entrypoint at build time, so a module the COPY set misses fails the build.
RUN python -c 'import themis.services.auth.__main__'
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime,
# so no chown is needed.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.auth"]
