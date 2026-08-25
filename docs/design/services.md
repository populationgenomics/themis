# Services: anatomy and how to add one

`themis/services/` is the internal **data plane** — gRPC services (HTTP/2, binary protobuf; no MCP, no REST), distinct
from `apps/` (the user-facing web surface). See [`../repo-structure.md`](../repo-structure.md) for where it sits.

**The server subclasses the servicer base class generated from a committed, hand-authored `.proto`.** The type system
then forces the rpc surface, so nothing has to be tested for agreement
([below](#the-forced-contract-the-generated-servicer-base)). The committed `.proto` is the contract; `buf breaking`
gates its evolution. `themis/services/auth/` is the worked example throughout — read it alongside this doc.

**Related:** [`proto.md`](proto.md) (authoring and evolving the contract), [`deployment.md`](deployment.md) (the stacked
deploy PR), [`migrations.md`](migrations.md) (tables and grants for a DB-backed service),
[`evidence-interfaces.md`](evidence-interfaces.md) (the multi-interface `evidence` deployment in practice),
[`../runbooks/add-a-service.md`](../runbooks/add-a-service.md) (the step-by-step checklist).

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
- **`tests/`** — behaviour tests against an in-process `grpc.aio` server, plus one for the entrypoint wiring. That
  server is `themis.testing.in_process_grpc.serving`, which yields a channel to wrap in the generated stub; don't
  hand-roll one.
- **`Dockerfile`** — multi-stage; build context is the repo root.

The messages, stub, and servicer base are **not** under the service — they live in the shared `themis/rpc/` (below),
because a caller imports the identical modules.

### One deployment, several interfaces

A deployment and a gRPC service are not one-to-one. `evidence` is one image and one Cloud Run service hosting several
independent gRPC services — its **interfaces** — so a caller pays one cold start and one deploy for the whole read
surface instead of one per source. Modularity is in the tree, not the deployment shape: interfaces share **libraries**;
they share no state, and no config beyond what `deps` carries (below). A shared library sits beside the interfaces (the
upstream clients under `themis/services/evidence/upstreams/`, say) — an upstream client, a status taxonomy, an HGVS
parser are the same code whichever interface calls them, and a second copy would drift. What no interface reaches is
another interface's backend, its adapter or its env vars — a convention reviews hold, not one a check enforces.

An interface is a **subpackage**, `themis/services/<name>/<domain>/`, holding the anatomy above — `servicer.py`,
`<port>.py`, `tests/` — with its `.proto` and generated stubs in the shared trees, exactly as a single-service
deployment has them. Plus:

- **`config.py`** — that interface's environment contract, and nothing else: which adapters each selector value builds,
  and the vars that configure them. Every var the interface reads carries the interface's name as a prefix, so no
  image-wide var decides an interface's behaviour. The selector inside that prefix is `THEMIS_<INTERFACE>_BACKEND` — one
  per interface, whatever it has behind it ([below](#adapters-an-abstract-port--pluggable-backends)).
- **`interface.py`** — `async register(server, deps)`: build the backend the interface's env selects, using the shared
  clients on `deps` (the image's `Deps`, built once in `deps.py`), and install the servicer. The whole seam. `register`
  is `async` so an adapter that needs `await` to build is possible at all.

**What every interface shares: `deps`.** Every interface that authorizes does so identically, resolving the same session
token through the same auth service. So `deps.py` builds one session resolver for the image, and with it a shared
`httpx.AsyncClient` — one connection pool against overlapping public hosts, not one per interface — and the image's
`contextlib.AsyncExitStack`, which owns whatever an interface's adapter holds open for the server's lifetime.

The resolver is the exception to the per-interface env rule: the vars selecting it and naming the auth service are
image-wide. A per-interface copy would be the same value written once per interface, with as many ways to set it
inconsistently. Its fixture seed is one var per *image* for the same reason, each image seeding the bindings its own
callers present; the shared factory is told which var the JSON came from, so it names that var when it rejects it.
(`literature` is the one interface that resolves no session: the corpus it serves is shared across analyses, not scoped
to one.)

Nothing in the data plane handles SIGTERM, so the exit stack unwinds on a startup failure, not on a Cloud Run stop — do
not register work there that has to run before the process dies until graceful drain exists.

`__main__.py` then holds no interface-specific code — a tuple of the `register` callables the image serves, `Deps` built
once, the health servicer, and `$PORT`. Health reports for the server as a whole, with no per-interface entry: an
interface that cannot build its backend exits the process, so a partial set never serves.

Adding one is that subpackage, an `INTERFACES` entry, and the repo wiring that a nested `tests/` dir and a new `COPY`
need ([Wiring into the repo](#wiring-into-the-repo); the runbook has the edits). Two of those would otherwise fail green
rather than red — an interface nothing registers, and a tests directory nothing collects — so tests hold both against
the tree ([`test_main.py`](../../themis/services/evidence/tests/test_main.py),
[`test_testpaths.py`](../../tests/test_testpaths.py)) instead of leaving them to a reviewer's eye.

**Scaling is per-instance, so interfaces share it.** Cloud Run replicates the whole image: load on one interface adds
instances that serve all of them, they contend for one instance's concurrency, and they scale to zero together
(`min_instance_count=0`). Statelessness is therefore a requirement, not a preference — any instance may serve any
request, and instances come and go, so an interface keeps no cross-request state in memory. A read-only handle held for
the server's lifetime is the exception, and belongs on the exit stack above; the fixture corpora are the shape to copy —
in-memory, but read-only and seeded identically from the environment, so every instance answers alike.

**IAM is per deployment, so interfaces share their callers too.** The invoker role is granted on the Cloud Run service,
and a caller holding it reaches every gRPC service the image serves — there is no per-interface grant to make. So
splitting a caller off the rpcs it has no business calling takes a **second deployment**, not a second interface: the
interface boundary is a code boundary, not an authorization one.

Reach for this only when the interfaces share a deploy boundary — one audience, one IAM posture, one scaling profile. A
service whose callers, credentials, or scaling differ stays its own deployment.

## The wire contract: proto → stubs

A service's contract is one hand-authored file under `schema/proto/themis/rpc/`, in package `themis.rpc.<domain>`, whose
`service` block names the operations. It is the `buf breaking` baseline and what the stubs are generated from;
[`proto.md`](proto.md) has the authoring rules and the pipeline, and the [runbook](../runbooks/add-a-service.md) the
sequence.

The package name doubles as the Python import path, so the committed stubs under `themis/rpc/` import each other with no
rewriting. A freshness gate fails CI when they drift from the `.proto`.

**One `Request` in, one `Response` out** — literally proto's `rpc Method(Request) returns (Response)`. A method returns
the domain resource when it maps to one (`ResolveSession → SessionContext`,
`GetWorkingDocument → WorkingDocumentSnapshot`), else a named `<Op>Response` (`PutWorkspace → PutWorkspaceResponse`).
Two carve-outs, both first-class in proto: a streaming payload is a `stream` of a chunk message (`PutWorkspace`
client-streams, `GetWorkspace` server-streams `WorkspaceChunk`), and a read whose only input is the implicit session
takes `google.protobuf.Empty` as its request message. The wire evolves additively (add a field, never renumber or remove
— retire with a `reserved` statement), so a generated caller never breaks; `buf breaking` flags a violation.

## The forced contract: the generated servicer base

The server subclasses `<domain>_pb2_grpc.<Service>Servicer` and implements each rpc. An unimplemented method or a wrong
message type is a static (pyright) error, and server and caller exchange the *same* generated message classes, so the
runtime API cannot drift from the contract. That is why there is no contract test: a generated servicer is the real
forced contract, not a stand-in for one.

Backward-compatibility is a separate gate. [`tools/schema/buf_compat.py`](../../tools/schema/buf_compat.py) diffs each
committed `.proto` against its base-branch baseline through a pinned `buf` Docker image, and fails on any incompatible
delta over the contracts it compares, with no in-tool override. A pre-release contract — no persisted data, no deployed
consumer — is held out of the comparison until it stabilises. It is the sole authored-data compat gate; see
[`proto.md`](proto.md), "Schema evolution".

## Adapters: an abstract port + pluggable backends

The servicer depends on the abstract port, not a concrete backend, so the same server runs offline (fixture) and
deployed (real). The port's methods are `async` — a blocking adapter (Cloud SQL, GCS) offloads its I/O to a thread
rather than stalling the `grpc.aio` event loop:

- **Selection** — one required env var per interface, `THEMIS_<INTERFACE>_BACKEND`; an unset or unknown value is a
  `SystemExit`, never a silent fallback. Where a deployment serves one interface, that interface's name is the
  service's, so `<INTERFACE>` and the service's own name are the same string; only a var scoped to the whole image
  carries the service name in its own right. One var however many ports sit behind it: literature reads stored full
  texts through one port and the public indexes through another, and the one var wires both at once. The split is how
  the interface is factored — a seam for testing, and for swapping a source later — not a knob an operator has any
  reason to turn. Half an interface offline against half live is a state nobody deploys on purpose and everybody
  debugging one has to rule out. One var per interface also keeps the vocabulary honest: a var selecting a single
  adapter can name the technology (`gcs`, `cloudsql`), while one standing for several names the mode — `live` against
  the real world, `fixture` against memory. `store` and `auth` are the outliers, `THEMIS_STORAGE_BACKEND` naming the
  port and `THEMIS_BACKEND` naming nothing; rename them when something else takes you into those files, rather than
  reading them as a second convention. The shared session resolver keeps its own image-wide selector,
  [above](#one-deployment-several-interfaces).
- **Fixture backend** — in-memory, for tests and a first deploy. Seed it *explicitly* from the environment, as one JSON
  document per interface with a named section per port where it has several, so the seed is as single a thing as the
  switch that selects it. The code never defaults to an empty or placeholder store: the caller (image, deploy, test)
  supplies the value, and `{}` is how it says "deliberately empty". An absent section is an error rather than an empty
  one — an empty list inside the document says "nothing here" deliberately, where a missing section says only that
  someone forgot. This is the fail-loud rule ([`../style/general.md`](../style/general.md)): a missing input raises, it
  does not limp along on a default.
- **Real backend** — the adapter the deploy selects; it arrives with the deploy PR, not before. A DB-backed backend
  additionally needs its tables and the migrate runner, which are cross-service and not the service PR's to define
  unilaterally (see deploy, below).

## Who calls a service

Two consumers, different shapes — know which a service is for:

- **The sandbox agent, in code mode** — the shape most *analysis* services take; `evidence` is the worked example. The
  agent writes code that calls the API and runs it under `bash` — full code mode, no CLIs, no discrete tool calls
  ([`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md)). It holds **no credential and no service URL**.
  The sandbox's only exit is a method-allowlisted gRPC hatch served by the trusted worker process outside it
  ([`../plans/postern-sandbox-swap.md`](../plans/postern-sandbox-swap.md)), which injects the session token as request
  metadata and forwards over a channel carrying the worker SA's ID token; the token lives only in the worker, so the
  agent can never present a valid one. The agent-facing client is therefore the **generated gRPC stub** pointed at the
  hatch — typed, one call per rpc, **fail-loud** (a `grpc.RpcError` surfaces, never a silent empty result). Which rpcs
  it reaches at all is decided by the `agent_exposed` option on the `.proto`, from which `regen` emits the hatch's
  allowlist ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)); absent the option, nothing.
- **The platform, service-to-service** — `auth` (called by every service to authorize a request) and `store` (the
  sandbox worker checkpoints `/workspace` to it) are consumed this way, never by the agent. The caller holds its own SA
  identity and presents its ID token: the generated stub over a channel built with `themis.clients.id_token`, wrapped
  for auth by `themis.clients.auth`.

**Sandbox-reachability is an explicit wiring step, not a default.** An agent-facing service is reached *through the
hatch*, so making it callable from the sandbox takes four things: the `agent_exposed` option on its `.proto`, a
forwarder on the hatch, its generated stub shipped into the guest's rootfs, and, at deploy time, the worker job's SA
holding `run.invoker` on it — internal services are IAM-gated rather than open. Platform services carry no option:
`auth` sits behind the store, reached only service-to-service, never by the sandbox. Decide which kind a service is
before the deploy PR.

Most analysis services are agent-facing; design their surface for the agent first.

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
  `UnresolvedSessionError` on a miss, so nothing calls a real auth and no path silently continues without a binding. The
  fixture ships its own metadata and id constants, which a test sends and asserts on in place of literals. Offline, an
  entrypoint builds a resolver from its seeding env var instead.
- **The pieces** (all under `themis.clients.auth`, usable apart): `session_resolver(auth_url)` builds a
  `SessionResolver` over the generated auth stub — presenting the SA ID token via `themis.clients.id_token`, and mapping
  auth's verdict on an unresolvable token to `UnresolvedSessionError` while every other failure (an auth outage, a
  timeout, an IAM misconfiguration) propagates rather than passing for a bad token; `require_session` is the servicer
  guard. Include the `session` dependency group.

The store is the worked example.

## Wiring into the repo

The [runbook](../runbooks/add-a-service.md) has the edits. Three of them are decisions rather than boilerplate:

- **The service's dependency group pins no `grpcio` version.** The runtime `grpcio` and the codegen toolchain's
  `grpcio-tools` resolve to one version out of the shared `uv.lock`, which is what keeps them compatible: a generated
  stub refuses at import to run against a `grpcio` older than the toolchain that emitted it.
- **A new `tests/` directory needs no ruff entry.** One `per-file-ignores` block, `themis/**/tests/**`, reaches every
  tests tree under `themis/` at any depth; a per-directory list meant a new interface's tests failed lint until someone
  remembered to add it. The generated `themis/rpc` tree is `extend-exclude`d once, so a new domain needs no edit there.
- **The image's build context is the repo root**, so the committed stubs ship, and it copies whole `themis/…` subtrees
  rather than files. A guard walks the entrypoint's transitive first-party imports and fails on one the image does not
  put on the import path ([`tests/test_image_contents.py`](../../tests/test_image_contents.py)).

Do **not** bake a working backend default into the image: the runtime requires each interface's
`THEMIS_<INTERFACE>_BACKEND` (and the fixture's seed) and exits at startup without them, and the deploy supplies them. A
baked fixture default would let a deploy that dropped the real override come up *serving* — an empty store answering
every lookup "not found", which reads as "genuinely absent" rather than a fault — so the fail-loud check must reach the
deployed revision.

## Deploy (a separate, stacked PR)

The service code PR ships the servicer running offline. The Pulumi Cloud Run deploy is a **separate stacked PR** — infra
review (IAM, secrets, Cloud Run) is a distinct concern from the service code; see [`deployment.md`](deployment.md) and
the repo's `infra/*` PRs. The Cloud Run service runs **HTTP/2 end to end** (gRPC needs it), with a gRPC startup probe
and IAM. Its ingress is as narrow as its callers allow: internal-only where every caller reaches it over the services
VPC, public-but-IAM-gated where one cannot — the web BFF has no VPC egress, so an internal-ingress service is
unreachable from it. Sequencing for a DB-backed service: infra attaches the service SA as a Cloud SQL DB user (connect +
authenticate only), while the table DDL and read/write grants are the **migrate runner's** job — a separate follow-up.
Do not define shared tables inside a single service's PR.

## Checklist

The step-by-step is the [add-a-service runbook](../runbooks/add-a-service.md), from authoring the proto through to the
deploy follow-up. This doc is the anatomy and the rationale behind those steps.
