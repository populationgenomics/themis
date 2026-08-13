# Services: anatomy and how to add one

`themis/services/` is the internal **data plane** — gRPC services (HTTP/2, binary protobuf; no MCP, no REST), distinct
from `apps/` (the user-facing web surface). See [`../repo-structure.md`](../repo-structure.md) for where it sits.

Load-bearing invariant: **the server subclasses the servicer base class generated from a committed, hand-authored
`.proto`.** The rpc surface is forced by the type system — an unimplemented rpc or a wrong message type is a static
error, not a runtime drift — so there is no contract test. The committed `.proto` is the contract; `buf breaking` gates
its evolution. `themis/services/auth/` is the worked example throughout — read it alongside this doc.

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
- `themis.migrate` — the SQL migration runner (see [`migrations.md`](migrations.md)).
- `themis.testing` — helpers shared by tests across packages. No production module imports it and no image copies it.

A gRPC service, its client helpers, and its generated `rpc` package share a domain name — the deployment's name where it
serves one service, the interface's where it serves several ([below](#one-deployment-several-interfaces)). Nothing else
is shared implicitly.

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
  `test_main.py` for the entrypoint wiring. `themis.testing.in_process_grpc.serving` is that server: it takes a callable
  that registers the servicer, and yields a channel to wrap in the generated stub — don't hand-roll one. No contract
  test — the servicer base is the contract.
- **`Dockerfile`** — multi-stage; build context is the repo root.

The messages, stub, and servicer base are **not** under the service — they live in the shared `themis/rpc/` (below),
because a caller imports the identical modules.

### One deployment, several interfaces

A deployment and a gRPC service are not one-to-one. `evidence` is one image and one Cloud Run service hosting several
independent gRPC services — its **interfaces** — so a caller pays one cold start and one deploy for the whole read
surface instead of one per fact source. Modularity is in the tree, not the deployment shape: nothing is shared between
interfaces beyond the server they attach to.

An interface is a **subpackage**, `themis/services/<name>/<domain>/`, holding the anatomy above — `servicer.py`,
`<port>.py`, `tests/` — with its `.proto` and generated stubs in the shared trees, exactly as a single-service
deployment has them. Plus:

- **`config.py`** — that interface's environment contract, and nothing else: which adapter each selector value builds,
  and the vars that configure it. Every var the interface reads carries its name as a prefix
  (`THEMIS_LITERATURE_FIXTURE`), so no image-wide var decides an interface's behaviour; within that prefix the selector
  follows the per-port rule below — `THEMIS_LITERATURE_BACKEND` for a one-port interface,
  `THEMIS_<INTERFACE>_<PORT>_BACKEND` where it has several. Each interface also names its own adapters — literature's
  are `fixture`/`live` — since no image-wide vocabulary can fit all of them.
- **`interface.py`** — `async register(server, stack)`: build the env-selected backend, install the servicer. The whole
  seam. `register` is `async` so an adapter that needs `await` to build is possible at all, and the `stack` (a
  `contextlib.AsyncExitStack`) owns the clients an interface holds for the server's lifetime. Nothing in the data plane
  handles SIGTERM, so that stack unwinds on a startup failure, not on a Cloud Run stop — do not register work there that
  has to run before the process dies until graceful drain exists.

`__main__.py` then holds no interface-specific code — a tuple of the `register` callables the image serves, the health
servicer, and `$PORT`. Health reports for the server as a whole, with no per-interface entry: an interface that cannot
build its backend exits the process, so a partial set never serves.

Adding one is that subpackage, an `INTERFACES` entry, its `testpaths` + `per-file-ignores` entries for the nested
`tests/` dir, and a `Dockerfile` COPY for any tree it reads. Two of those would otherwise fail green, so tests hold
them: `themis/services/evidence/tests/test_main.py` (every `interface.py` in the tree is registered, and every
registration is an `interface.py`) and `tests/test_testpaths.py` (every tests directory is collected).

**Scaling is per-instance, so interfaces share it.** Cloud Run replicates the whole image: load on one interface adds
instances that serve all of them, they contend for one instance's concurrency, and they scale to zero together
(`min_instance_count=0`). Statelessness is therefore a requirement, not a preference — any instance may serve any
request, and instances come and go, so an interface keeps no cross-request state in memory. A read-only handle held for
the server's lifetime is the exception, and belongs on the `stack` above; the fixture corpora are the shape to copy —
in-memory, but read-only and seeded identically from the environment, so every instance answers alike.

Reach for this only when the interfaces genuinely share a deploy boundary (one audience, one IAM posture, one scaling
profile). A service whose callers, credentials, or scaling differ stays its own deployment.

## The wire contract: proto → stubs

The `.proto` is the source of truth for the shapes and the service, hand-authored; see [`proto.md`](proto.md) for the
authoring rules. Per service:

1. Author `schema/proto/themis/rpc/<domain>.proto` — `package themis.rpc.<domain>`, a `service` whose rpcs are the
   operations, and messages whose every field carries an explicit number. `stream` marks a streaming rpc; a retired
   field name or number goes in a `reserved` statement.
1. Run `uv run python -m tools.schema.regen`. It runs `buf generate` to the committed `themis/rpc/<domain>_pb2.py`,
   `.pyi`, and `<domain>_pb2_grpc.py` stubs.

The committed **`.proto` is the source of truth** for a service: the contract, the `buf breaking` baseline, and what
`buf generate` produces the stubs from. The package name doubles as the Python import path (`themis.rpc.<domain>`), so
the stubs emit `from themis.rpc import <domain>_pb2` with no import rewriting. The freshness gate fails CI if the
committed stubs drift from the `.proto` — after any `.proto` change, re-run `regen` and commit.

**One `Request` in, one `Response` out** — literally proto's `rpc Method(Request) returns (Response)`. A method returns
the domain resource when it maps to one (`resolveSession → SessionContext`,
`getWorkingDocument → WorkingDocumentSnapshot`), else a named `<Op>Response` (`putWorkspace → PutWorkspaceResponse`).
Two carve-outs, both first-class in proto: a streaming payload is a `stream` of a chunk message (`putWorkspace`
client-streams, `getWorkspace` server-streams `WorkspaceChunk`), and a read whose only input is the implicit session
takes `google.protobuf.Empty` as its request message. The wire evolves additively (add a field, never renumber or remove
— retire with a `reserved` statement), so a generated caller never breaks; `buf breaking` enforces it.

## The forced contract: the generated servicer base

The server subclasses `<domain>_pb2_grpc.<Service>Servicer` and implements each rpc. An unimplemented method or a wrong
message type is a static (pyright) error, and server and caller exchange the *same* generated message classes — so the
runtime API cannot drift from the contract, and there is no separate contract test — a generated servicer is the real
forced contract, not a stand-in for one. Backward-compatibility is the separate `buf breaking` gate:
[`tools/schema/buf_compat.py`](../../tools/schema/buf_compat.py) diffs each committed `.proto` against its base-branch
baseline through a pinned `buf` Docker image — advisory (a sign, not a merge cop). It is the sole authored-data compat
gate (the at-rest `chuckd` gate was retired). See [`proto.md`](proto.md), "Schema evolution".

## Adapters: an abstract port + pluggable backends

The servicer depends on the abstract port, not a concrete backend, so the same server runs offline (fixture) and
deployed (real). The port's methods are `async` — a blocking adapter (Cloud SQL, GCS) offloads its I/O to a thread
rather than stalling the `grpc.aio` event loop:

- **Selection** — a required env var per port, named after the port (`THEMIS_<PORT>_BACKEND`: store's
  `THEMIS_STORAGE_BACKEND` and `THEMIS_AUTHORIZER_BACKEND`, `hello`'s `THEMIS_AUTHORIZER_BACKEND`, the evidence image's
  `THEMIS_LITERATURE_BACKEND`); an unset or unknown value is a `SystemExit`, never a silent fallback. Having one port is
  no exception — `hello` has one and still names it. `auth` predates the rule and reads a bare `THEMIS_BACKEND`: an
  outlier to rename when something else takes you into that file, not a second convention. Each port names the adapters
  it actually has (`gcs`/`fixture`, `http`/`fixture`, `live`/`fixture`), so no service-wide switch forces one vocabulary
  on all of them.
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
  `UnresolvedSessionError` on a miss, so nothing calls a real auth and no path silently continues without a binding. In
  tests that is `themis.clients.auth.tests.fixture_session.resolve_fixture_session`; its `GOOD_METADATA`, `PROJECT_ID`,
  and `ANALYSIS_ID` are what a test sends and asserts on, in place of the literals. Offline, an entrypoint builds one
  from its seeding env var through `session.fixture_session_resolver_from_json`.
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
- `[tool.ruff]` — the generated `themis/rpc` tree is `extend-exclude`d once (protoc's output is not lint-clean), so a
  new domain needs no `extend-exclude` change. A new `tests/` directory does need its own `per-file-ignores` entry: the
  patterns are root-anchored, so the `"tests/**"` entry does not reach it and every `assert` trips `S101`.

`Dockerfile` (copy `themis/services/auth/Dockerfile`) — multi-stage; **build context is the repo root** so the committed
stubs ship; deps from the committed `uv.lock` via `uv sync --locked --group <name>` (the age-gated lock the whole repo
uses); `COPY` the `themis/rpc/<domain>_pb2*` stubs plus the `themis/…` subtrees the service needs; `PYTHONPATH=/app`;
Cloud Run injects `$PORT`. Do **not** bake a working backend default into the image: the runtime requires each port's
`THEMIS_<PORT>_BACKEND` (and the fixture's seed) and exits at startup without them, and the deploy supplies them. A
baked fixture default would let a deploy that dropped the real override come up *serving* — an empty store answering
every lookup "not found", which reads as "genuinely absent" rather than a fault — so the fail-loud check must reach the
deployed revision (auth, store, hello, and evidence all follow this).

## Deploy (a separate, stacked PR)

The service code PR ships the servicer running offline. The Pulumi Cloud Run deploy is a **separate stacked PR** — infra
review (IAM, secrets, Cloud Run) is a distinct concern from the service code; see [`deployment.md`](deployment.md) and
the repo's `infra/*` PRs. The Cloud Run service runs **HTTP/2 end to end** (gRPC needs it) with a gRPC health check,
internal ingress, and IAM. Sequencing for a DB-backed service: infra attaches the service SA as a Cloud SQL DB user
(connect + authenticate only), while the table DDL and read/write grants are the **migrate runner's** job — a separate
follow-up. Do not define shared tables inside a single service's PR.

## Checklist

The step-by-step is the [add-a-service runbook](../runbooks/add-a-service.md): author the proto → `regen` → servicer +
port + fixture + `__main__` → tests → wire `pyproject.toml`/`Dockerfile`/`.github/images.json` → service-to-service and
agent-exposure where they apply → validate → the deploy follow-up. This doc is the anatomy and rationale behind those
steps.
