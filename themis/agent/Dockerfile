# syntax=docker/dockerfile:1

# The untrusted sandbox agent image (self-hosted-sandbox.md §6). Entrypoint is the Anthropic
# `ant beta:worker run` worker, which reads ANTHROPIC_SESSION_ID / _WORK_ID / _ENVIRONMENT_ID from the
# per-execution env the dispatcher injects and honors ANTHROPIC_BASE_URL (pointed at the proxy). Build
# context is the repo ROOT. It ships a Python runtime + the generated gRPC stubs so the model can call
# internal services in code mode, and the working-document linter it runs to self-correct.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ant: the Managed-Agents worker CLI (release binary; curl only at build time).
FROM base AS ant
ARG ANT_VERSION=1.15.0
# SHA256 of the release tarballs (ant_${ANT_VERSION}_checksums.txt); bump both alongside ANT_VERSION.
ARG ANT_SHA256_AMD64=184c5af95c4e9bc40805268b3f44e74a62288f3bba65e7731c992c09adf9a4c2
ARG ANT_SHA256_ARM64=f0e54d79766ac4617e6aad9b272d697b3dbf71d26e90a7b6e653267ae2e5bd7d
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo amd64) \
    && SHA=$([ "$TARGETARCH" = "arm64" ] && echo "$ANT_SHA256_ARM64" || echo "$ANT_SHA256_AMD64") \
    && curl -fsSL -o /tmp/ant.tar.gz "https://github.com/anthropics/anthropic-cli/releases/download/v${ANT_VERSION}/ant_${ANT_VERSION}_linux_${ARCH}.tar.gz" \
    && echo "${SHA}  /tmp/ant.tar.gz" | sha256sum -c - \
    && tar -xz -C /usr/local/bin -f /tmp/ant.tar.gz ant \
    && rm /tmp/ant.tar.gz

# build: the code-mode Python deps (grpcio + the generated stubs the model imports).
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
COPY --from=ant /usr/local/bin/ant /usr/local/bin/ant
COPY --from=build /app/.venv /app/.venv
# A placeholder environment key (the CLI requires one; the proxy replaces the Authorization header, so
# the value is never valid); ANTHROPIC_BASE_URL points at the proxy's localhost HTTP port.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    ANTHROPIC_BASE_URL=http://127.0.0.1:8080 \
    ANTHROPIC_ENVIRONMENT_KEY=placeholder-replaced-by-proxy
COPY themis/rpc /app/themis/rpc
COPY themis/document_linter /app/themis/document_linter
COPY themis/agent/services.py /app/themis/agent/services.py
WORKDIR /workspace
VOLUME /mnt/session/outputs
ENTRYPOINT ["ant", "beta:worker", "run"]
