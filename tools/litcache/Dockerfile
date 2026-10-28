# syntax=docker/dockerfile:1

# The litcache seed-ingestion Dataflow worker image. Build context is the repo ROOT.
# Runner v2 execs the Beam SDK bootloader (grafted from the matching official image); the
# bootloader starts the harness in the image's default (system) python, so the uv-locked
# `dataflow` dependency group (apache-beam[gcp] pinned to the launcher's version + the
# litcache runtime + the Cloud SQL connector) is installed there rather than in a venv the
# bootloader would not see. litcache source is provided on PYTHONPATH (the workspace root is
# not installed as a package, matching the other service images).
FROM apache/beam_python3.13_sdk:2.77.0 AS beam

# A named stage rather than `COPY --from=<image>`: Dependabot updates images on FROM lines only.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# ca-certificates, plus the RHEL-canonical bundle path native libcurl clients probe (the
# Debian bundle lives at /etc/ssl/certs; the path some GCS-reading C libs expect does not
# exist here otherwise). media-types supplies /etc/mime.types: the slim image omits it, so
# without it Python's mimetypes can't map extensions absent from its built-in table (.xlsx,
# .docx, …), which litfetch's fetchers consult when the server serves no media type.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    media-types \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /etc/pki/tls/certs \
    && ln -s /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt
# The base image ships the standard library without bytecode, and PYTHONDONTWRITEBYTECODE stops a process caching
# its own; each image compiles its /app tree the same way after its last COPY.
RUN python -m compileall -q /usr/local/lib/python3.13

FROM base AS build
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
# The locked dataflow deps as a flat requirements file, project excluded (litcache source is
# grafted on PYTHONPATH in the runtime stage, not installed as a package).
RUN uv export --locked --no-emit-project --group dataflow -o requirements.txt

FROM base AS runtime
COPY --from=build /app/requirements.txt /tmp/requirements.txt
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
# uv is bind-mounted, not copied: the installer has no business in the shipped worker.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --system -r /tmp/requirements.txt
# The SDK harness bootstrapper Dataflow execs to start the worker (must match the SDK
# version installed above).
COPY --from=beam /opt/apache/beam /opt/apache/beam
COPY themis/common /app/themis/common
COPY themis/litcache /app/themis/litcache
# The generated protovalidate stubs the litcache proto imports (a repo-root package).
COPY buf /app/buf
RUN python -m compileall -q /app
ENV PYTHONPATH=/app
# Fail the build (not a live worker) if the pipeline's imports are unsatisfiable.
RUN python -c "import apache_beam, themis.litcache.ingest_beam, themis.litcache.cloudsql"
ENTRYPOINT ["/opt/apache/beam/boot"]
