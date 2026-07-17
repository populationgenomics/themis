# syntax=docker/dockerfile:1

# The untrusted sandbox agent image (self-hosted-sandbox.md §6). Entrypoint is the first-party Python
# worker (themis/agent/worker.py), which reads ANTHROPIC_SESSION_ID / _WORK_ID / _ENVIRONMENT_ID from the
# per-execution env the dispatcher injects and honors ANTHROPIC_BASE_URL (pointed at the proxy). Build
# context is the repo ROOT. It ships a Python runtime + the generated gRPC stubs so the model can call
# internal services in code mode, and the working-document linter it runs to self-correct.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# build: the code-mode Python deps (grpcio + the generated stubs the model imports) and the anthropic SDK
# the worker's EnvironmentWorker + custom shell tool are built on.
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group agent --python 3.13

FROM base AS runtime
COPY --from=build /app/.venv /app/.venv
# A placeholder environment key (the SDK requires a bearer; the proxy replaces the Authorization header, so
# the value is never valid); ANTHROPIC_BASE_URL points at the proxy's localhost HTTP port.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    ANTHROPIC_BASE_URL=http://127.0.0.1:8080 \
    ANTHROPIC_ENVIRONMENT_KEY=placeholder-replaced-by-proxy
COPY themis/rpc /app/themis/rpc
COPY themis/document_linter /app/themis/document_linter
COPY themis/agent/services.py /app/themis/agent/services.py
COPY themis/agent/worker.py /app/themis/agent/worker.py
WORKDIR /workspace
VOLUME /mnt/session/outputs
ENTRYPOINT ["python", "-m", "themis.agent.worker"]
