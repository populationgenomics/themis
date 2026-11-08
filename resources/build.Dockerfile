# syntax=docker/dockerfile:1

# The reference-build Cloud Run Job: mirror resources/reference.toml's pins, fetch the Entrez status
# tables, then cut hgvs-weaver-data stores and genomes (tools/resources/job.py). Build context is the
# repo ROOT (deploy: `docker build -f resources/build.Dockerfile .`). Dependencies come from the
# committed uv.lock via `uv sync --locked --group resources`. The manifest is baked in, so the image
# built from a commit mirrors and builds exactly that commit's pins.
# A named stage rather than `COPY --from=<image>`: Dependabot updates images on FROM lines only.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The base image ships the standard library without bytecode, and PYTHONDONTWRITEBYTECODE stops a process caching
# its own; each image compiles its /app tree the same way after its last COPY.
RUN python -m compileall -q /usr/local/lib/python3.13

# build: install the locked resources group into /app/.venv with a pinned uv. The root project is
# virtual (no [build-system]), so uv sync installs deps only.
FROM base AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group resources --python 3.13

FROM base AS runtime
# bagz's GCS plugin (hgvs-weaver-data's gcs extra) loads libcurl at import, and native libcurl looks for its CA
# bundle at the RHEL path, which Debian does not populate.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libcurl4 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/pki/tls/certs \
    && ln -s /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY resources/reference.toml ./resources/reference.toml
COPY tools/__init__.py ./tools/__init__.py
COPY tools/resources ./tools/resources
# -x skips the venv's site-packages, which uv compiled at install.
RUN python -m compileall -q -x /site-packages/ /app
# Run unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime, no chown. Scratch is the
# system temporary directory, which Cloud Run backs with memory.
RUN useradd --system --uid 10001 app
USER app
# The Job supplies `--project` as its args, which Cloud Run appends to the entrypoint in place of a CMD.
ENTRYPOINT ["python", "-m", "tools.resources.job"]
