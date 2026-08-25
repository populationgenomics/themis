# Runbook: add a service

Add a backend service under `themis/services/` — the mechanical sequence. Anatomy, the wire-contract rules, and why each
piece exists: [`services.md`](../design/services.md) (read it first if the service is your first). Proto authoring:
[`proto.md`](../design/proto.md). Exposing a service to the sandbox agent:
[`sandbox-rpc-exposure.md`](../design/sandbox-rpc-exposure.md).

`<domain>` is the proto package leaf (`themis.rpc.<domain>`); `<name>` is the service package
(`themis.services.<name>`). They usually match.

## Prerequisites

- The repo builds offline: `uv sync` clean, `pre-commit install` done.
- `buf` on `PATH` (regen and the freshness gate need it) — see [`proto.md`](../design/proto.md).
- `bun install --cwd apps/web` done — regen's protobuf-es leg runs unconditionally and needs the local `protoc-gen-es`
  plugin.
- You know which consumer the service is for — the **sandbox agent** or **platform service-to-service**
  ([`services.md`](../design/services.md), "Who calls a service"). It decides whether you do step 6.

## 1. Author the wire contract

Write `schema/proto/themis/rpc/<domain>.proto`: `package themis.rpc.<domain>`, a `service` whose rpcs are the
operations, one `Request` in and one `Response` out per rpc, an explicit number on every message field, `stream` for a
streaming rpc, `reserved` for any retired name/number. Authoring rules: [`proto.md`](../design/proto.md).

```sh
$EDITOR schema/proto/themis/rpc/<domain>.proto
uv run --group codegen python -m tools.schema.regen
```

Commit the `.proto`, the generated `themis/rpc/<domain>_pb2.py`, `.pyi`, `_pb2_grpc.py`, and the protobuf-es output
`apps/web/src/gen/themis/rpc/<domain>_pb.ts`. The freshness gate stages the whole tree, so an uncommitted stub — Python
or TypeScript — fails CI.

## 2. Scaffold the service package

`themis/services/<name>/`:

- **`servicer.py`** — the `<Service>Servicer` subclass, one method per rpc over the generated messages; takes its
  backend (the abstract port) and a `themis.clients.auth.session.SessionResolver` as constructor arguments. Authorize
  inbound at the top of each method — `session = await session.require_session(context, self._session_resolver)` before
  touching scoped state; it aborts `UNAUTHENTICATED`/`PERMISSION_DENIED` and never returns `None`.
- **`<port>.py`** — the backend port as an `abc.ABC`, plus an in-memory **fixture** implementation for offline runs.
- **`__main__.py`** — builds its backends from one **required** env var (`THEMIS_<INTERFACE>_BACKEND`; unset/unknown ⇒
  `SystemExit`, never a silent default), registers the servicer + a `grpc.health.v1` health servicer on a `grpc.aio`
  server, serves on `$PORT`.

Fail-loud seeding: the fixture is seeded explicitly from the environment (JSON env var), never a code default — `{}` is
a deliberate empty store ([`general.md`](../style/general.md)).

Adding an **interface to an existing deployment** instead of a new service — a fact source under `evidence` — scaffolds
`themis/services/<name>/<domain>/` with a `config.py` + `interface.py`, its own nested `tests/`, and one entry in the
entrypoint's `INTERFACES`. It writes no `__main__.py`, and adds no `Dockerfile` or `.github/images.json` entry (step 4),
but it does edit the existing image's: a `COPY` for any tree it reads, and its deps into that image's dependency group.
Step 4's `testpaths` edit names the nested `tests/` path. See [`../design/services.md`](../design/services.md) §"One
deployment, several interfaces".

## 3. Tests

`themis/services/<name>/tests/` — behaviour tests over `themis.testing.in_process_grpc.serving`, authorized with
`themis.clients.auth.tests.fixture_session` (its `GOOD_METADATA` / `PROJECT_ID` / `ANALYSIS_ID`), plus `test_main.py`
for the entrypoint wiring. No contract test — the generated servicer base is the contract.

## 4. Wire the repo

Root `pyproject.toml`:

- `[dependency-groups]` — add `<name> = [...]` (`grpcio`, `grpcio-health-checking`, `protobuf`, and
  `{ include-group = "session" }` for the inbound auth client); include it in the `test` and `lint` groups. No version
  specifier — the runtime `grpcio` and the codegen toolchain's `grpcio-tools` resolve to one version out of the shared
  `uv.lock` ([`services.md`](../design/services.md), "Wiring into the repo").
- `[tool.pytest.ini_options]` — append `themis/services/<name>/tests` to `testpaths`.
- `[tool.ruff.lint.per-file-ignores]` — no edit. `"themis/**/tests/**"` already reaches a new service's tests at any
  depth.

`Dockerfile` (copy `themis/services/evidence/Dockerfile`): multi-stage, build context the repo root.

- `uv sync --locked --group <name>`, then `COPY` the `themis/rpc/<domain>_pb2*` stubs plus the `themis/…` subtrees the
  service needs; `PYTHONPATH=/app`.
- `RUN python -c 'import <entrypoint>.__main__'`, so the image's own interpreter resolves the entrypoint at build — the
  complement of the pre-merge import walk in [`tests/test_image_contents.py`](../../tests/test_image_contents.py).
- Bake **no** default for a backend selector or its seed. The runtime must exit without them, so a deploy that drops the
  real override fails loud instead of serving an empty store ([`services.md`](../design/services.md), "Wiring into the
  repo").

`.github/images.json` — add a `{ "name", "context", "file", "env", "runtime" }` entry for the image. This one list is
what the `images` build-check (every Dockerfile still builds) and `deploy` both read; a service with no entry is never
built. Add the matching `_<NAME>_IMAGE_ENV = 'THEMIS_<NAME>_IMAGE'` constant in `infra/__main__.py` in the same PR — not
the deploy follow-up — because [`tests/test_images_manifest.py`](../../tests/test_images_manifest.py) holds the entry to
both the tracked Dockerfile set and those constants, so `pytest` (step 7) fails otherwise.

## 5. Service-to-service calls (if any)

If the service calls another, use that service's generated stub over a channel built with `themis.clients.id_token` (the
caller SA's ID token). Do not hand-roll the channel or the token. Inbound authorization is step 2's `require_session`,
not this outbound leg.

## 6. Expose to the sandbox agent (agent-facing services only)

Skip for platform (worker-only) services. Exposure is the `agent_exposed` option on the service's `.proto`, and it is
fail-closed: `regen` derives the hatch's method allowlist from the files carrying it
([`tools/schema/agent_exposed.py`](../../tools/schema/agent_exposed.py)), and a proto without the option reaches the
guest with nothing. The steps, the rest of the surface the option drives, and the threat model — an exposed RPC must
assume a hostile caller — are in [`sandbox-rpc-exposure.md`](../design/sandbox-rpc-exposure.md) ("In practice").

## 7. Validate

```sh
uv sync --group test && uv run pytest
uv sync --group lint && uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run --group codegen python -m tools.schema.regen   # tree stays clean (freshness green)
```

`uv run` re-syncs to the group it is given, so each line names its group (per `README.md`/CI). `buf breaking` runs in
CI; run it locally if you changed an existing `.proto`.

## 8. Follow-up PRs

- **Deploy** — the Pulumi Cloud Run service is a **separate stacked PR** (infra review is a distinct concern);
  [`deployment.md`](../design/deployment.md).
- **Real backend / DB** — a DB-backed backend needs its tables and grants from the **migrate runner** (cross-service,
  not this PR's to define; [`migrations.md`](../design/migrations.md)).
- **Sandbox reachability** — an agent-facing service's deploy also needs its network/IAM path to the worker; see the
  deploy PR and [`sandbox-rpc-exposure.md`](../design/sandbox-rpc-exposure.md).
