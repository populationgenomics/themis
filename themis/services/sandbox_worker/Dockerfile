# syntax=docker/dockerfile:1

# The self-hosted sandbox worker image (postern-sandbox-swap.md §4). Build context is the repo ROOT
# (`docker build -f themis/services/sandbox_worker/Dockerfile .`). One trusted process: it holds the
# Anthropic environment key and the session token, runs the EnvironmentWorker loop, and marshals every
# `shell` command into a bubblewrap sandbox whose only exit is the store/hello-forwarding hatch.
#
# Four stages: worker deps (uv), the curated guest rootfs, then assemble. The guest rootfs is bound
# read-only at runtime by postern (SandboxProfile(rootfs='/opt/guest-root')) — no runtime container
# engine; Docker builds it at *build* time.
FROM python:3.13-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- worker deps: postern[grpc] + anthropic + the internal-service client, from the committed lock. ---
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group sandbox_worker --python 3.13

# --- the guest rootfs: ONLY what sandboxed model code needs — the interpreter, grpcio, and the guest
#     SDK. This stage's whole filesystem becomes the guest's world, so the guest sees none of the
#     worker's userland or credentials. ---
FROM base AS guest
# Install the guest's grpcio/protobuf from the committed lock (not a floating pip install) so it inherits the
# 7-day age gate and the same pins as the worker. The lock + uv are bind-mounted, not copied, so no build
# tooling lands in the rootfs; --only-group guest + --no-deps install exactly the locked set.
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/uvproj/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/uvproj/uv.lock \
    --mount=from=ghcr.io/astral-sh/uv:0.11.16,source=/uv,target=/usr/local/bin/uv \
    uv export --frozen --no-emit-project --only-group guest --no-hashes --project /tmp/uvproj \
    | pip install --no-cache-dir --no-deps -r /dev/stdin
# The guest SDK: the generated hello stubs the model dials the hatch with (`from themis.rpc import
# hello_pb2_grpc`), the code-mode `services` helper (shipped as `themis.agent.services` — the stable name
# the agent's prompt imports), and the stdlib-only working-document linter. Only the hello stubs ship — the
# store stubs would be an unused capability, since the guest reaches the store through no hatch method.
COPY themis/rpc/hello_pb2.py themis/rpc/hello_pb2_grpc.py /usr/local/lib/python3.13/site-packages/themis/rpc/
COPY themis/document_linter /usr/local/lib/python3.13/site-packages/themis/document_linter
COPY themis/services/sandbox_worker/guest/services.py /usr/local/lib/python3.13/site-packages/themis/agent/services.py
RUN touch /usr/local/lib/python3.13/site-packages/themis/__init__.py \
    /usr/local/lib/python3.13/site-packages/themis/agent/__init__.py

# --- assemble: bubblewrap + the worker venv + the themis subtrees + the guest rootfs. ---
FROM base AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app
COPY themis/rpc ./themis/rpc
COPY themis/clients/id_token.py ./themis/clients/id_token.py
COPY themis/clients/work_queue/__init__.py themis/clients/work_queue/client.py ./themis/clients/work_queue/
COPY themis/services/sandbox_worker ./themis/services/sandbox_worker
# postern binds /opt/guest-root/{usr,lib,...} read-only as the guest's system dirs (copying '/'
# preserves the usrmerge symlink layout). Cloud Run gen2 provides the unprivileged user namespaces
# bubblewrap needs; the worker's verify() gate refuses to serve if they are absent.
COPY --from=guest / /opt/guest-root
CMD ["python", "-m", "themis.services.sandbox_worker"]
