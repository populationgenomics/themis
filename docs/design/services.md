# Design: Python gRPC services

**Related:** [`proto.md`](proto.md) (protobuf contracts, code generation, and compatibility checks),
[`evidence-interfaces.md`](evidence-interfaces.md) (how the multi-interface `evidence` service applies this layout),
[`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) (how `.proto` options expose RPCs to the sandbox agent),
[`sandbox-worker.md`](sandbox-worker.md) (the worker hatch that forwards sandbox RPCs),
[`sheaf-service.md`](sheaf-service.md) (the workspace repository gRPC service), [`migrations.md`](migrations.md) (schema
DDL and runtime database grants), [`deployment.md`](deployment.md) (Cloud Run deployment and image promotion),
[`security.md`](security.md) (IAM, service-to-service authentication, and ingress controls),
[`../runbooks/add-a-service.md`](../runbooks/add-a-service.md) (step-by-step runbook for adding a service or interface).

## Overview

The Python packages under [`themis/services/`](../../themis/services/) form the internal data plane. Every service
speaks gRPC over HTTP/2 with binary protobuf payloads, while [`apps/`](../../apps/) serves the user-facing web tier.

When a data plane grows across several data sources and storage backends, two failure modes appear quickly: callers and
servers drift on wire types, and services silently fall back to empty or half-configured backends when deployment
configuration is incomplete. To prevent wire drift, every service implements the servicer base class generated from a
committed `.proto` file under [`schema/proto/themis/rpc/`](../../schema/proto/themis/rpc/) and exchanges the generated
message classes directly with its callers. To prevent silent misconfiguration, each servicer depends on an abstract port
whose concrete adapter (`fixture` for offline runs and tests, or a live backend for Cloud Run) is selected at startup by
a required environment variable with no default baked into the image.

Services that share callers, IAM permissions, and scaling characteristics can also share a single deployment as
independent subpackages called interfaces, as [`themis/services/evidence/`](../../themis/services/evidence/) does across
its genomic and literature data sources. Whether a service runs alone in its own container or alongside peer interfaces,
it uses the same package anatomy, port boundary, and fail-loud startup rules.

## Background

All first-party Python code lives under a single top-level [`themis/`](../../themis/) directory configured as a PEP 420
namespace package (see [`../repo-structure.md`](../repo-structure.md)). Because `themis/` has no top-level `__init__.py`
and the repository root is the sole `pythonpath` entry, each service's `Dockerfile` can copy only the subtrees it
imports (`themis/rpc/`, its own `themis/services/<name>/` tree, and any `themis/clients/` helpers) and Python resolves
every `themis.*` import identically in local tests and in containers.

```
themis/
├── rpc/                  # generated *_pb2.py and *_pb2_grpc.py (shared by servers and callers)
├── clients/              # client transport and auth helpers (id_token.py, auth/)
├── services/
│   ├── auth/             # single-interface service deployment
│   └── evidence/         # multi-interface service deployment
│       ├── clinvar/
│       ├── literature/
│       └── ...
├── migrate/              # SQL migration runner
└── testing/              # test-only helpers (in_process_grpc.py; never copied into images)
```

A gRPC domain links three packages under the same name: the generated protobuf messages and stubs in
`themis.rpc.<domain>_pb2` and `themis.rpc.<domain>_pb2_grpc`, the server implementation in `themis.services.<name>`, and
any client-side helpers in `themis.clients.<name>`. When a deployment serves a single gRPC service (such as
[`themis/services/auth/`](../../themis/services/auth/) or [`themis/services/sheaf/`](../../themis/services/sheaf/)), the
domain name matches the deployment name. When a single deployment hosts several gRPC services (such as
[`themis/services/evidence/`](../../themis/services/evidence/)), each hosted interface owns its own domain name.

## Design

### Wire contract and generated servicer base

If a server defines its own request or response classes or translates protobuf messages into an intermediate transport
layer before reaching the handler, the server and caller can disagree on field names or types unless a separate suite of
contract tests keeps them aligned. Instead, each gRPC service is defined once in a hand-authored `.proto` file under
[`schema/proto/themis/rpc/`](../../schema/proto/themis/rpc/) in package `themis.rpc.<domain>` (see
[`proto.md`](proto.md)). Because the protobuf package name matches the Python module path, the committed outputs under
[`themis/rpc/`](../../themis/rpc/) import each other without path rewriting.

Every RPC takes one `Request` message and returns one `Response` message:

- When an operation returns a domain resource, the response message is that resource (`ResolveSession` returns
  `SessionContext`, and `ReadRefDoc` returns `RefDocSnapshot`).
- When an operation does not map to a single resource, the response is a named `<Op>Response` message (`Publish` returns
  `PublishResponse`).
- Streaming payloads return `stream <Chunk>` (`FetchPack` server-streams `PackChunk`), and read operations whose only
  input is the caller's session take `google.protobuf.Empty` (`ReadRefDoc`).

The server subclasses `<domain>_pb2_grpc.<Service>Servicer` and implements each RPC method directly. If a method is
missing or returns the wrong type, static type checking (`pyright`) fails before any test runs. Wire compatibility
across commits is enforced separately by [`tools/schema/buf_compat.py`](../../tools/schema/buf_compat.py), which diffs
each committed `.proto` file against the base branch using `buf breaking` and rejects field removals or renumberings.

### Single-service anatomy

[`themis/services/auth/`](../../themis/services/auth/) illustrates a single-interface deployment:

- **`servicer.py`** subclasses the generated `<Service>Servicer` with one `async` method per RPC. It receives its
  backend through its constructor typed as the abstract port, so the servicer never imports a concrete storage client.
- **`<port>.py`** defines that abstract port as an `abc.ABC` (for `auth`, `AuthBackend` in
  [`themis/services/auth/backend.py`](../../themis/services/auth/backend.py)). Its methods are `async` and correspond
  directly to the RPCs the servicer handles.
- **`fixture.py`** implements the in-memory backend for offline runs and tests, along with the seed document vocabulary
  and parser (for `auth`, [`themis/services/auth/fixture.py`](../../themis/services/auth/fixture.py)).
- **`<adapter>.py`** implements the live backend used in Cloud Run, with one module per upstream data source. For
  `auth`, `LiveBackend` in [`themis/services/auth/live.py`](../../themis/services/auth/live.py) reads the token store
  through [`cloudsql.py`](../../themis/services/auth/cloudsql.py) and the ACL through
  [`secret.py`](../../themis/services/auth/secret.py).
- **`__main__.py`** reads the environment, constructs the selected backend, registers the servicer and the standard
  `grpc.health.v1` health servicer on a `grpc.aio` server, and listens on Cloud Run's `$PORT`.
- **`tests/`** runs behavioral tests against an in-process `grpc.aio` server using
  [`themis/testing/in_process_grpc.py`](../../themis/testing/in_process_grpc.py) (`serving`), which yields a channel
  that tests wrap in the generated stub, alongside a test for `__main__.py` startup wiring.
- **`Dockerfile`** builds a multi-stage image using the repository root as the Docker build context.

### One deployment, several interfaces

Deploying a separate Cloud Run service for every read-only genomic dataset (`clinvar`, `gnomad`, `vep`, `literature`,
and peers) would multiply container images, IAM bindings, and cold starts across services that share the same callers
and scaling profile. Instead, [`themis/services/evidence/`](../../themis/services/evidence/) packages ten independent
gRPC services as **interfaces** inside a single image and Cloud Run deployment (see
[`evidence-interfaces.md`](../../docs/design/evidence-interfaces.md)).

```
themis/services/evidence/
├── __main__.py           # iterates INTERFACES, starts grpc.aio server + health servicer
├── deps.py               # builds shared Deps once (session resolver, httpx2 client, exit stack)
├── upstreams/            # shared upstream HTTP/API clients used by multiple interfaces
├── clinvar/              # one interface subpackage
│   ├── interface.py      # async register(server, deps)
│   ├── config.py         # reads THEMIS_CLINVAR_BACKEND and adapter vars
│   ├── servicer.py
│   ├── backend.py
│   ├── fixture.py
│   └── tests/
└── literature/           # peer interface subpackage (never imports from clinvar/)
```

Modularity lives in the package tree rather than in the deployment topology. Interfaces share helper libraries placed
beside them (such as upstream clients under
[`themis/services/evidence/upstreams/`](../../themis/services/evidence/upstreams/), error-status helpers, or HGVS
parsers), because duplicating those clients would cause behavior to drift across interfaces. However, no interface
imports another interface's port, adapter, or environment variables. Similarly, each service package is self-contained:
it imports `themis/rpc/` for the generated stubs and shared domain libraries under `themis/` (such as `themis/sheaf/`,
`themis/litcache/`, or `themis/clients/`), and nothing from another service's package, `tools/`, or `infra/`. This is a
convention, not enforced by a linter; the review check is that a service's imports stay within its own package,
`themis/rpc/`, and those shared libraries in `themis/`.

Each interface subpackage `themis/services/<name>/<domain>/` contains the standard service modules (`servicer.py`,
`<port>.py`, `fixture.py`, `<adapter>.py`, and `tests/`) plus two glue modules:

- **`config.py`** defines that interface's environment contract: which adapter each selector value builds and which
  environment variables configure it. Every variable read by an interface is prefixed with the interface name (headed by
  `THEMIS_<INTERFACE>_BACKEND`), ensuring that no image-wide variable alters an individual interface's backend.
- **`interface.py`** exposes `async register(server, deps)`, which builds the backend selected by `config.py` using the
  shared clients on `deps` and registers the servicer on `server`. Making `register` `async` allows adapters to perform
  asynchronous initialization at startup when needed.

#### Shared image dependencies (`deps.py`)

While interfaces keep their domain backends separate, every interface that authorizes requests resolves session tokens
against the same `auth` service, and several interfaces call overlapping external HTTP hosts. Building a separate auth
channel and HTTP connection pool per interface would waste sockets and duplicate configuration across every interface in
the image.

Instead, [`themis/services/evidence/deps.py`](../../themis/services/evidence/deps.py) builds a single `Deps` container
once per process:

- **Authorizer (`Deps.authorizer`):** selected by the image-wide `THEMIS_AUTHORIZER_BACKEND` environment variable in
  [`deps.py`](../../themis/services/evidence/deps.py) and configured with the `auth` service URL, the GCP project and
  audience URL, and the fixture seed JSON. Using image-wide variables avoids repeating identical auth settings across
  the hosted interface prefixes. When seeding fixture sessions or callers, each image reads its own seed variables and
  passes those variable names to the shared parser so validation errors cite the exact environment variable that failed.
  The `evidence` and `sheaf` data-plane servers are constructed through a process-wide `AuthInterceptor` backed by the
  configured authorizer ([`__main__.py`](../../themis/services/evidence/__main__.py),
  [`deps.py`](../../themis/services/evidence/deps.py)), so caller verification, session resolution, and `admits_caller`
  admission are enforced across every hosted interface before any handler runs (see
  [`rpc-authorization.md`](rpc-authorization.md); the `hello` smoke-test service instead checks its session in-handler
  via `require_session`). Within `evidence`, the agent-called RPCs (`admits_caller = CALLER_AGENT`) require a resolved
  session, whereas `literature`'s three browser-called RPCs (`DescribePaper`, `ResolveContent`, and `Locate`, marked
  `admits_caller = CALLER_WEB`) admit the web tier on its service-account ID token without requiring a session.
- **Shared HTTP client (`httpx2.AsyncClient`):** provides a single connection pool across interfaces that query external
  HTTP endpoints.
- **Process lifetime stack (`contextlib.AsyncExitStack`):** owns long-lived read-only handles opened by interface
  adapters during `register`. Data-plane services do not install custom `SIGTERM` handlers, so this exit stack unwinds
  on startup failures rather than during Cloud Run container shutdown; adapters must not rely on it for pre-termination
  flush work until graceful drain exists.

[`themis/services/evidence/__main__.py`](../../themis/services/evidence/__main__.py) holds no interface-specific logic:
it builds `Deps` once, loops over a tuple of `register` callables (`INTERFACES`), attaches a single `grpc.health.v1`
servicer for the whole server, and listens on `$PORT`. If any interface fails to build its backend, the process exits
immediately so Cloud Run never routes traffic to a partially initialized server. Two repository tests prevent silent
omissions when adding an interface:
[`themis/services/evidence/tests/test_main.py`](../../themis/services/evidence/tests/test_main.py) verifies that every
interface subpackage appears in `INTERFACES`, and [`tests/test_testpaths.py`](../../tests/test_testpaths.py) verifies
that every `tests/` directory is registered for `pytest` collection.

#### Costs of sharing a deployment

Grouping interfaces into one Cloud Run service trades independent scaling and IAM isolation for operational simplicity:

- **Shared instance scaling and statelessness:** Cloud Run scales whole container instances (`min_instance_count=0`).
  Load on one interface scales out all interfaces together, and concurrent calls across interfaces share each instance's
  event loop and memory. Every interface must therefore remain stateless across requests; only read-only handles opened
  at startup or deterministic in-memory fixture corpora seeded from the environment may persist across calls.
- **Shared Cloud Run IAM boundary:** `roles/run.invoker` is granted on the Cloud Run service as a whole, so any caller
  permitted to invoke the deployment can reach every gRPC service registered inside it at the IAM layer. Separating
  callers with different trust postures requires a separate Cloud Run deployment, not another interface subpackage.

Two gRPC services share a deployment only when they share their scaling envelope, their IAM grants, and their failure
domain, and splitting them later would move nothing a caller sees. When any of those diverge — a heavy conversion worker
beside a fast read path, or a capability that needs an IAM grant the others should not hold — they live in separate
deployments from the start.

### Abstract ports and pluggable backends

Every servicer talks to an abstract port (`abc.ABC`) with `async` methods. When a live adapter uses a synchronous
library (such as a blocking database driver or GCS client), the adapter offloads its blocking I/O to a worker thread so
it never stalls the `grpc.aio` event loop.

#### One port and one selector per interface

When an interface's live backend reads from multiple upstream systems — for example, `literature` fetches full-text
markdown from GCS and resolves paper identifiers against a Postgres crosswalk table — it is tempting to define a
separate port and selector variable for each upstream source. Doing so splits the interface along internal
implementation seams that have no independent caller: only the interface's servicer calls the port, and no operator ever
intentionally deploys half of an interface against live storage and the other half against an in-memory fixture.

Each interface therefore defines a **single port** matching the RPCs its servicer serves, and selects its implementation
through a **single required environment variable**, `THEMIS_<INTERFACE>_BACKEND` (for a single-interface service,
`<INTERFACE>` is the service name; `auth` is the outlier, its `THEMIS_BACKEND` naming no capability — rename it when
something else takes you into those files, rather than reading it as a second convention). Where the live adapter uses a
single backing technology, the selector value can name that technology (`gcs`, `cloudsql`); where the live adapter
composes multiple sources behind the port, the selector names the operating mode (`live` vs. `fixture`). If
`THEMIS_<INTERFACE>_BACKEND` is unset or set to an unknown value, the process exits at startup with `SystemExit`. An
abstraction behind the port earns its place only when something can vary behind it on its own — a second interface, a
batch job, or a caller outside the service. Absent one of those, which sources an adapter reads is the adapter's
business, and one port per interface keeps the servicer a thin translation layer.

#### Fixture isolation and fail-loud seeding

Fixture mode is a deployable runtime mode shipped in the production container image so a new service or interface can be
deployed and exercised end-to-end before its live backing store is provisioned. However, the fixture class, the schema
of its JSON seed document, and the seed parser live in `fixture.py` rather than in the port module. Keeping `fixture.py`
separate ensures that readers of the port module see only the domain contract, and that live adapters never depend on
fixture seed vocabulary. In production code, only `config.py` (or `__main__.py` in a single-interface service) imports
`fixture.py`. A service that does not yet have this shape — one port, with `fixture.py` separated from `backend.py` — is
brought to it when next substantially edited, rather than in a sweep of its own.

When an interface runs in fixture mode, its seed data must be supplied explicitly through the environment as a single
JSON document per interface with one top-level key per port method. Neither `fixture.py` nor the container image
provides a default empty store, and omitting a method's section in the JSON document raises an error at startup rather
than defaulting to an empty list (`{}` or `[]` inside the section is how a caller marks a collection as intentionally
empty). Similarly, a `Dockerfile` must never bake `THEMIS_<INTERFACE>_BACKEND=fixture` or a default fixture seed into
the image: if a production deployment accidentally dropped its environment configuration, a baked fixture default would
boot cleanly and return "not found" responses that look like genuine domain absences instead of failing the startup
probe.

### Callers, outbound RPCs, and session resolution

```
┌───────────────────────────┐        ┌──────────────────────────┐
│ Sandbox guest (untrusted) │        │ Trusted platform caller  │
│  generated gRPC stub      │        │  (apps/web, worker, etc.)│
└─────────────┬─────────────┘        └────────────┬─────────────┘
              │ unix socket / loopback            │ HTTPS / HTTP/2
              ▼                                   │ + SA ID token
┌───────────────────────────┐                     │ (+ optional CallerClaim)
│ Sandbox worker hatch      │                     │
│  checks admits_caller     │                     │
│  injects CallerClaim      │                     │
│  attaches worker ID token │                     │
└─────────────┬─────────────┘                     │
              │ HTTPS / HTTP/2                    │
              ▼                                   ▼
┌───────────────────────────────────────────────────────────────┐
│ Target gRPC service (e.g. evidence, sheaf)                    │
│  AuthInterceptor verifies ID token + CallerClaim              │
│  and resolves session via themis.clients.auth ────────────────┼──► auth service
└───────────────────────────────────────────────────────────────┘
```

#### Sandbox callers vs. platform callers

A service's `.proto` file is written for its primary caller — usually the agent — and reviewed as an agent-facing
interface before any web or operator method is added to it. Services in [`themis/services/`](../../themis/services/)
serve two distinct caller populations:

1. **The sandbox agent in code mode:** Most analysis services (such as `evidence`) are called by Python scripts the
   agent writes and runs inside the sandbox through the worker's method-allowlisted gRPC hatch, which attaches a
   `CallerClaim` (`CALLING_AS_AGENT_SESSION` plus the session token) and forwards the call under the worker service
   account's OIDC ID token ([`sandbox-worker.md`](sandbox-worker.md#the-hatch-is-the-capability-boundary)). How RPCs are
   annotated with `option (themis.rpc.admits_caller) = CALLER_AGENT` and wired through the hatch allowlist, guest
   rootfs, and Cloud Run IAM is specified in
   [`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md#the-rpc-is-the-unit-of-exposure). An RPC that does not name
   `CALLER_AGENT` is left out of the generated hatch allowlist
   ([`agent_exposed.py`](../../tools/schema/agent_exposed.py)), and the interceptor admits only the callers an RPC
   names. Adding a method to a `.proto` file therefore never exposes it by accident.
1. **Trusted platform services:** Services such as `auth` (called by other services to resolve session tokens) and
   `sheaf` (called by `apps/web` as `CALLER_WEB` and by the sandbox worker's file mirror as `CALLER_SANDBOX_WORKER`; see
   [`sheaf-service.md`](sheaf-service.md)) are called service-to-service and never admit `CALLER_AGENT`.

#### Outbound service-to-service calls

When one service or platform component calls another gRPC service, it uses the callee's generated stub from
`themis.rpc.<domain>_pb2_grpc` over a channel authenticated with
[`themis/clients/id_token.py`](../../themis/clients/id_token.py). Calling `id_token.channel_credentials(callee_url)`
builds composite gRPC channel credentials that combine TLS with the caller service account's ID token minted from the
metadata server for `audience=callee_url` and refreshed automatically before expiry. Because the generated stub already
provides a typed, one-call-per-RPC interface that raises `grpc.RpcError` on failure, callers use the stub directly
rather than wrapping it in a custom client class.

#### Authorization and session resolution via `themis.clients.auth`

The `evidence` and `sheaf` data-plane servers are constructed through a process-wide `AuthInterceptor`
([`interceptor.py`](../../themis/clients/auth/interceptor.py)) backed by the configured authorizer
(`THEMIS_AUTHORIZER_BACKEND` in [`deps.py`](../../themis/services/evidence/deps.py)), so admission on those services is
enforced before any handler runs (see [`rpc-authorization.md`](rpc-authorization.md); the `hello` smoke-test service
instead checks its session in-handler via `require_session`). On every inbound RPC, `AuthInterceptor` verifies the
caller's Google OIDC ID token, decodes the caller's claim, resolves any attached session token through the `auth`
service, checks the resulting `AuthContext` against the method's `admits_caller` protobuf option, and binds
`AuthContext` in `contextvars` for the handler's lifetime.

- **Session resolution** calls the `auth` service over an ID-token-authenticated channel. When `auth` reports that a
  token is unknown or expired, the resolver raises an unresolved-session error that the interceptor turns into
  `PERMISSION_DENIED`; if `auth` fails for any other reason (such as a network timeout, service outage, or IAM
  misconfiguration), the underlying `grpc.RpcError` propagates so infrastructure faults are never mistaken for invalid
  caller tokens.
- **Offline and test runs** select the fixture authorizer via `THEMIS_AUTHORIZER_BACKEND=fixture` (seeded from
  environment variables in offline deployments or constructed with fixture constants in unit tests).

### Repository wiring and deployment boundaries

Adding a service or interface requires keeping the repository's toolchain, container build, and infrastructure stacks in
sync (see [`../runbooks/add-a-service.md`](../runbooks/add-a-service.md) for the exact file edits):

- **Single lockfile for `grpcio` and `grpcio-tools`:** A service's dependency group in `pyproject.toml` never pins a
  separate `grpcio` version. Generated `*_pb2_grpc.py` files check at import time that the runtime `grpcio` version is
  at least as new as the `grpcio-tools` version that generated them; resolving both from the shared `uv.lock` guarantees
  they stay compatible.
- **Globbed lint and format exclusions:** Ruff's `per-file-ignores` uses `themis/**/tests/**` to cover test directories
  at both `themis/services/<name>/tests/` and `themis/services/<name>/<interface>/tests/`, and `extend-exclude` ignores
  the generated `themis/rpc` tree globally, so adding a new service or interface requires no linter configuration
  changes.
- **Verified container import trees:** Because each `Dockerfile` copies explicit `themis/...` subtrees from the
  repository root, forgetting to copy a newly imported first-party package would pass unit tests and fail only when the
  container boots. [`tests/test_image_contents.py`](../../tests/test_image_contents.py) prevents this by walking each
  service entrypoint's transitive first-party imports and verifying that every imported path is copied by the
  corresponding `Dockerfile`.
- **Stacked deployment and migration PRs:** A service lands with its fixture backend and contract tests; the real
  backend arrives with — or after — the PR that deploys the service against the store it reads, so an adapter is never
  written against a schema or bucket layout that exists only on paper. Provisioning the Cloud Run service happens in a
  separate stacked Pulumi PR (see [`deployment.md`](deployment.md)) so IAM, secret bindings, and networking are reviewed
  independently from application logic. Every Cloud Run gRPC service is configured for end-to-end HTTP/2 with a gRPC
  startup probe and takes no direct public internet traffic: internal callers reach it over the VPC, and services that
  maintainers also invoke by hand add an operator backend on the external load balancer behind IAP restricted to the
  automation account (see [`security.md`](security.md#ingress-from-the-public-internet)). For database-backed services,
  the Pulumi PR grants the service account Cloud SQL connection and authentication rights, while schema DDL
  (`CREATE TABLE`) and table-level `SELECT`/`INSERT` grants are applied separately by the migration runner (see
  [`migrations.md`](migrations.md)). Where two services will share a database table, the table's migration lands in its
  own PR before either adapter is written against it, rather than being defined inside whichever service happens to land
  first.
