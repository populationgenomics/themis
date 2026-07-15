# Services: anatomy and how to add one

`themis/services/` is the internal **data plane** — gRPC services (HTTP/2, binary protobuf; no MCP, no REST), distinct
from `apps/` (the user-facing web surface). See [`../repo-structure.md`](../repo-structure.md) for where it sits.

Load-bearing invariant: **the server subclasses the servicer base class generated from a committed, TypeSpec-authored
`.proto`.** The interface is forced by the type system — an unimplemented rpc or a wrong message type is a static error,
not a runtime drift — so there is no contract test. The committed `.proto` is the contract; `buf breaking` gates its
evolution. `themis/services/auth/` is the worked example throughout — read it alongside this doc.

This is a playbook: follow the sections top-to-bottom (they are the build order), or jump to the
[checklist](#checklist).

## Packaging: one `themis` namespace

Everything importable lives under a single top-level `themis/` tree (a PEP 420 namespace — no top-level `__init__.py`,
so an image copies only the subtrees it needs and the import still resolves). The repo root is the only `pythonpath`
entry.

- `themis.rpc` — the **generated** protobuf messages, gRPC stubs, and servicer bases: `themis.rpc.<domain>_pb2` and
  `themis.rpc.<domain>_pb2_grpc` per domain (flat files under `themis/rpc/`), imported by the server and every caller.
- `themis.services.<name>` — a service's server implementation.
- `themis.clients.<name>` — the client-side helpers for *calling* service `<name>` (see
  [Who calls a service](#who-calls-a-service)).
- `themis.migrate` — the SQL migration runner.

A service, its client helpers, and its generated `rpc` package share a domain name; nothing else is shared implicitly.

## Anatomy

A service is `themis/services/<name>/`, the package `themis.services.<name>`:

- **`servicer.py`** — the servicer class subclassing the generated `<Service>Servicer`, one method per rpc taking and
  returning the generated proto messages. It takes its backend as a constructor argument — it depends on the abstract
  port base, never a concrete backend.
- **`<port>.py`** — the backend port as an `abc.ABC` (auth's is `backend.SessionBackend`), plus its implementations: an
  in-memory **fixture** for offline runs, and the real adapter later.
- **`__main__.py`** — the server entrypoint. Builds the backend from the environment (selected by a required env var —
  fail loud, no silent default), registers the servicer and a `grpc.health.v1` health servicer on a `grpc.aio` server,
  and serves on Cloud Run's `$PORT`.
- **`tests/`** — behaviour tests against an in-process `grpc.aio` server (or the servicer methods directly), plus
  `test_main.py` for the entrypoint wiring. No contract test — the servicer base is the interface.
- **`Dockerfile`** — multi-stage; build context is the repo root.

The messages, stub, and servicer base are **not** under the service — they live in the shared `themis/rpc/` (below),
because a caller imports the identical modules.

## The wire contract: TypeSpec → proto → stubs

One source of truth for the shapes and the service, authored in TypeSpec with `@typespec/protobuf`; see
[`typespec.md`](typespec.md) for the authoring rules. Per service:

1. Author `schema/<domain>/main.tsp` — `@package({ name: "themis.rpc.<domain>" })` on the namespace, a
   `@TypeSpec.Protobuf.service` interface whose operations are the rpcs, and messages whose every field carries a
   `@field(n)` number. `@stream(StreamMode.In | Out | Duplex)` marks a streaming rpc; `@reserve` retires a field name or
   number. Auto-discovered by `regen` (globs `schema/*/main.tsp`); no registration step.
1. Run `uv run --group codegen python -m tools.schema.regen`. It compiles the committed
   `schema/proto/themis/rpc/<domain>.proto` (the contract) and runs `protoc` (grpcio-tools) to the committed
   `themis/rpc/<domain>_pb2.py` + `<domain>_pb2_grpc.py` stubs.

The committed **`.proto` is the only committed schema artifact** for a service: the contract, the `buf breaking`
baseline, and the source `protoc` generates from. The package name doubles as the Python import path
(`themis.rpc.<domain>`), so `protoc` emits `from themis.rpc import <domain>_pb2` with no import rewriting. The freshness
gate fails CI if the committed proto or stubs drift from the `.tsp` — after any `.tsp` change, re-run `regen` and
commit.

**One `Request` in, one `Response` out** — literally proto's `rpc Method(Request) returns (Response)`. A method returns
the domain resource when it maps to one (`resolveSession → SessionContext`,
`getWorkingDocument → WorkingDocumentSnapshot`), else a named `<Op>Response` (`putWorkspace → PutWorkspaceResponse`).
Two carve-outs, both first-class in proto: a streaming payload is a `@stream` of a chunk message (`putWorkspace`
client-streams, `getWorkspace` server-streams `WorkspaceChunk`), and a read whose only input is the implicit session
takes no request model — the emitter maps a paramless op to `google.protobuf.Empty`. The wire evolves additively (add a
field, never renumber or remove — retire with `@reserve`), so a generated caller never breaks; `buf breaking` enforces
it.

## The forced interface: the generated servicer base

The server subclasses `<domain>_pb2_grpc.<Service>Servicer` and implements each rpc. An unimplemented method or a wrong
message type is a static (pyright) error, and server and caller exchange the *same* generated message classes — so the
runtime API cannot drift from the contract, and there is no separate contract test — a generated servicer is the real
forced interface, not a stand-in for one. Backward-compatibility is the separate `buf breaking` gate:
[`tools/schema/buf_compat.py`](../../tools/schema/buf_compat.py) diffs each committed `.proto` against its base-branch
baseline through a pinned `buf` Docker image — advisory (a sign, not a merge cop). It is the sole authored-data compat
gate (ADR 0003 retired the at-rest `chuckd` gate). See [`typespec.md`](typespec.md), "Schema evolution".

## Adapters: an abstract port + pluggable backends

The servicer depends on the abstract port, not a concrete backend, so the same server runs offline (fixture) and
deployed (real). The port's methods are `async` — a blocking adapter (Cloud SQL, GCS) offloads its I/O to a thread
rather than stalling the `grpc.aio` event loop:

- **Selection** — `__main__` reads the backend from a required env var (`THEMIS_BACKEND`); an unset or unknown value is
  a `SystemExit`, never a silent fallback.
- **Fixture backend** — in-memory, for tests and a first deploy. Seed it *explicitly* from the environment (auth:
  `THEMIS_FIXTURE_BINDINGS`, JSON). The code never defaults to an empty or placeholder store; the caller (image, deploy,
  test) supplies the value, `{}` for a deliberate empty store. This is the fail-loud rule
  ([`../style/general.md`](../style/general.md)): a missing input raises, it does not limp along on a default.
- **Real backend** — lands later, usually with the deploy. A DB-backed backend additionally needs its tables and the
  migrate runner, which are cross-service and not the service PR's to define unilaterally (see deploy, below).

## Who calls a service

Two consumers, different shapes — know which a service is for:

- **The sandbox agent, in code mode** — the shape most *analysis* services take (`litcache`, and the genomics/compute
  APIs to come). The agent writes code that calls the API and runs it under `bash` — full code mode, no CLIs, no
  discrete tool calls ([`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md)). It holds **no credential
  and no service URL**: a sandbox-local proxy injects the session token (as `x-themis-session-token` metadata) and the
  callee's `run.invoker` ID token (as the `authorization` metadata), and forwards from `localhost` — the session token
  lives only in the proxy, so the agent can never present a valid one. So the agent-facing client is the **generated
  gRPC stub** pointed at the local proxy — typed, one call per rpc, **fail-loud** (a `grpc.RpcError` surfaces, never a
  silent empty result). A new service *just appears* by shipping its `themis.rpc.<domain>` stub + a skill doc into the
  image.
- **The platform, service-to-service** — `auth` (called by every service to authorize a request) and `store` (the proxy
  checkpoints `/workspace` to it) are consumed this way, never by the agent. The caller holds its own SA identity and
  presents its ID token: the generated stub over a channel built with `themis.clients.id_token`, wrapped for auth by
  `themis.clients.auth`.

**Sandbox-reachability is an explicit wiring step, not a default.** An agent-facing service is reached *through the
proxy*, so making it callable from the sandbox means adding it to the *sandbox-reachable services* list in the sandbox
Pulumi module: a Cloud DNS response-policy allow for its name, an egress-route allow, a proxy forward-route, and the
sandbox job SA's `run.invoker` on it (internal services are IAM-gated, not open —
[`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md) §7–§8). Platform services are **not** on that list:
`auth` sits behind the store, reached only service-to-service, never by the sandbox. Decide which kind a service is
before the deploy PR.

The services that exist today (`auth`, `store`) are both platform infra; most services to come are agent-facing. Design
an analysis service's surface for the agent first.

## Calling another service (service-to-service): the generated stub

A service that calls another — the store resolves a session token through auth — neither hand-rolls a channel nor
re-declares the callee's shapes. It imports the callee's **generated stub** from `themis.rpc.<domain>` (the same package
the server subclasses) and builds a channel with the shared credential primitive:

- **`themis.clients.id_token`** — the internal-call transport primitive. `id_token.channel_credentials(callee_url)`
  returns composite channel credentials: TLS plus the runtime SA's ID token (audience = the callee URL) as per-call
  credentials, minted from the metadata server and refreshed on expiry. Cloud Run validates the ID token's audience, so
  a plain call is rejected. Shared across every internal caller.
- **The call** — `stub = <domain>_pb2_grpc.<Service>Stub(channel); stub.Method(request)`. The stub is already the typed,
  one-call-per-rpc, fail-loud surface (`grpc.RpcError` on failure), so no hand-written wrapper sits over it — only the
  credential wiring and, where a call has a domain-specific expected outcome, a thin mapping (auth, below).

For the near-universal case — resolving a request's session through auth — that wiring is already built:

## Authorizing a request via `themis.clients.auth`

Every data-plane service authorizes a request by resolving its session token to a Project + Analysis through the auth
service. Don't rebuild it — `themis.clients.auth` layers this on the generated auth stub:

- **In the servicer** — resolve the session once, at the top of each method:
  ```python
  self._session_resolver = session_resolver(auth_url)   # or a fixture SessionResolver in tests

  async def PutWorkingDocument(self, request, context):
      session = await require_session(context, self._session_resolver)   # the binding, else aborts the RPC
      version = await self._storage.put_working_document(session.analysis_id, request.markdown)
      return store_pb2.PutWorkingDocumentResponse(version=version)
  ```
  `require_session` reads the `x-themis-session-token` metadata (the bearer never surfaces as a message field), resolves
  it, and `context.abort`s `UNAUTHENTICATED` on a missing token or `PERMISSION_DENIED` on one that does not resolve. It
  never returns `None`: a servicer cannot proceed without a binding.
- **In tests / offline** — pass a fixture `SessionResolver` that returns a `SessionContext` or **raises**
  `UnresolvedSessionError` on a miss, so nothing calls a real auth and no path silently continues without a binding.
- **The pieces** (all under `themis.clients.auth`, usable apart): `session_resolver(auth_url)` builds a
  `SessionResolver` over the generated auth stub — presenting the SA ID token via `themis.clients.id_token`, mapping any
  resolve failure to `UnresolvedSessionError`; `require_session` is the servicer guard. Include the `session` dependency
  group.

The store is the worked example.

## Wiring into the repo

Root `pyproject.toml`:

- `[dependency-groups]` — add `<name> = [...]` (`grpcio`, `grpcio-health-checking`, `protobuf`); include it in the
  `test` and `lint` groups. `grpcio` and the `codegen` group's `grpcio-tools` are pinned to the **same** version — the
  generated stubs hard-check the runtime `grpcio` version.
- `[tool.pytest.ini_options]` — append `themis/services/<name>/tests` to `testpaths`. `pythonpath` stays `["."]`; the
  namespace resolves from the repo root (`consider_namespace_packages = true`).
- `[tool.ruff]` — the generated `themis/rpc` tree is `extend-exclude`d once (protoc's output is not lint-clean); a new
  domain needs no ruff change.

`Dockerfile` (copy `themis/services/auth/Dockerfile`) — multi-stage; **build context is the repo root** so the committed
stubs ship; deps from the committed `uv.lock` via `uv sync --locked --group <name>` (the age-gated lock the whole repo
uses); `COPY` the `themis/rpc/<domain>_pb2*` stubs plus the `themis/…` subtrees the service needs; `PYTHONPATH=/app`;
Cloud Run injects `$PORT`. Set explicit `ENV` defaults for every required var — the image is a caller, so it supplies
required inputs rather than relying on a code default (auth pins `THEMIS_BACKEND=fixture` and
`THEMIS_FIXTURE_BINDINGS="{}"` so the image boots).

## Deploy (a separate, stacked PR)

The service code PR ships the servicer running offline. The Pulumi Cloud Run deploy is a **separate stacked PR** — infra
review (IAM, secrets, Cloud Run) is a distinct concern from the service code; see [`deployment.md`](deployment.md) and
the repo's `infra/*` PRs. The Cloud Run service runs **HTTP/2 end to end** (gRPC needs it) with a gRPC health check,
internal ingress, and IAM. Sequencing for a DB-backed service: infra attaches the service SA as a Cloud SQL DB user
(connect + authenticate only), while the table DDL and read/write grants are the **migrate runner's** job — a separate
follow-up. Do not define shared tables inside a single service's PR.

## Checklist

1. `schema/<domain>/main.tsp` — author the wire contract ([`typespec.md`](typespec.md) authoring rules):
   `@Protobuf.package`, a `@Protobuf.service` interface, `@field(n)` on every message field, `@stream` for streaming;
   one `Request` in, one `Response` out.
1. Run `regen`; commit `schema/proto/themis/rpc/<domain>.proto` + the `themis/rpc/<domain>_pb2*.py` stubs.
1. `themis/services/<name>/` — `servicer.py` (the `<Service>Servicer` subclass), the backend `abc.ABC` + fixture,
   `__main__` (env-selected backend, `grpc.aio` server + health servicer, fail-loud seeding).
1. `themis/services/<name>/tests/` — behaviour tests (in-process `grpc.aio`), `test_main.py`.
1. Wire root `pyproject.toml` (dep group + `test`/`lint` include, `testpaths`) and the `Dockerfile`.
1. If it calls another service, use that service's generated stub over `themis.clients.id_token`; auth is
   `themis.clients.auth`.
1. Validate: `uv run pytest`; `pyright`; `ruff`; `regen` → clean tree (freshness green); `buf breaking` clean.
1. Follow-up PRs: the Cloud Run deploy, and any DB migrate-runner work the real backend needs.
