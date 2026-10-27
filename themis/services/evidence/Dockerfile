# syntax=docker/dockerfile:1

# Multi-stage build of the evidence gRPC service. Build context is the repo ROOT
# (deploy: `docker build -f themis/services/evidence/Dockerfile .`), so the committed
# themis/rpc stubs ship in the image. Dependencies come from the committed uv.lock via
# `uv sync --locked --group evidence`. Cloud Run injects $PORT; __main__ binds [::]:$PORT
# and serves gRPC over HTTP/2.
# A named stage rather than `COPY --from=<image>`: Dependabot updates images on FROM lines only.
FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv
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
    uv sync --locked --group evidence --python 3.13

FROM base AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# No backend is baked in: each interface requires its own backend selector (and, for a
# fixture, its non-empty seed) and exits at startup without them. A baked fixture default would let a
# deploy that dropped the live override come up SERVING an empty corpus — every paper "not found", which
# reads as "genuinely absent" rather than a fault — so the fail-loud check must reach the deployed revision.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/rpc ./themis/rpc
COPY themis/evidence/models ./themis/evidence/models
# The image reaches into litcache for the manifest stubs, the readiness derivation, the manifest key,
# the crosswalk lookup, and the conversion enqueue, and litcache reaches themis/common for storage +
# the Cloud SQL dial — so both packages ship whole rather than as a hand-listed subset that silently
# rots as the interfaces grow. buf carries the vendored validate stubs litcache_pb2 imports.
COPY buf ./buf
COPY themis/common ./themis/common
COPY themis/litcache ./themis/litcache
# The session client every interface but literature authorizes through. `themis.clients.id_token` is a
# module of that namespace package, not a package, so it copies as a file.
COPY themis/clients/auth ./themis/clients/auth
COPY themis/clients/id_token.py ./themis/clients/id_token.py
# The interfaces share the library's SVCv4 vocabularies rather than copying them: the 50-nt NMD rule,
# the P/LP classification terms and gold-star range, the gene-disease validity classes.
COPY themis/svcv4 ./themis/svcv4
COPY themis/services/evidence ./themis/services/evidence
# -x skips the venv's site-packages, which uv compiled at install.
RUN python -m compileall -q -x /site-packages/ /app
# Import the entrypoint at build time: a module the COPY set misses is otherwise only found when the
# deployed revision crash-loops, since nothing else imports the image's own tree.
RUN python -c 'import themis.services.evidence.__main__'
# Serve unprivileged (defense-in-depth): the venv + copied trees are read-only at runtime.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "themis.services.evidence"]
