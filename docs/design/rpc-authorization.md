# Design: data-plane RPC authorization

**Related:** [`security.md`](security.md) (the default-deny chokepoint rule this implements for gRPC),
[`sandbox-worker.md`](sandbox-worker.md) (the hatch every call from the sandbox agent crosses),
[`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) (the `admits_caller = CALLER_AGENT` proto option that drives guest
stub generation), [`evidence-fulltext.md`](evidence-fulltext.md) (the paper-conversion spend that `MaybeIngestPapers`
gates), [`literature-evidence-layer.md`](literature-evidence-layer.md) (the literature interface and shared corpus),
[`sheaf-service.md`](sheaf-service.md) (the workspace repository service that gates streaming RPCs with this
interceptor)

## Overview

The **data plane** is the set of backend gRPC services under `themis/services/` that serve the evidence an Analysis
draws on. Historically, data-plane RPCs authorized callers in one way: the handler read a session token from call
metadata, resolved it through the **auth service** (the service that issues and resolves session tokens), and discarded
the resulting Project and Analysis binding. That check answered only whether the caller held a valid session. On the
literature interface, read RPCs did not even run that check because the **web tier** (the BFF server behind the browser)
reads the shared paper **corpus** using its own Cloud Run service-account identity without a session token, leaving
agent-facing literature reads completely ungated.

A boolean session check cannot express who may call an RPC when services have callers without sessions, callers that
share a service account across trust boundaries, and calls that exercise a human user's institutional subscription.
Authorization in [`themis/clients/auth/`](../../themis/clients/auth/) instead assembles the facts each call carries into
an immutable `AuthContext` and checks that context against an admission rule declared on the RPC in protobuf:

1. **Caller** (`caller` and `calling_as`): the first-party GCP service account verified from the Cloud Run
   `Authorization` ID token, paired with a metadata **claim** stating whether the account is calling as itself
   (`CALLING_AS_SELF`), forwarding a call from the sandbox agent (`CALLING_AS_AGENT_SESSION`), or calling from the
   sandbox worker (`CALLING_AS_WORKER_SESSION`). Every internal call carries a verified caller.
1. **Binding** (`session`): the Project and Analysis resolved from a session token via the auth service. Present
   whenever the claim names a session.
1. **Person**: the human user and approved paper scope resolved from an approval token granted in the browser. Present
   only when an acquisition call draws on a user's institutional entitlement.

A server-wide gRPC interceptor builds `AuthContext` before any handler runs and checks it against the principals
declared on the method's contract in [`sandbox_options.proto`](../../schema/proto/themis/rpc/sandbox_options.proto) via
the repeated `admits_caller` method option. Each principal in the `Caller` enum binds a service account ID to a required
`CallingAs` mode (`CALLER_WEB`, `CALLER_CLU`, `CALLER_AGENT`, `CALLER_SANDBOX_WORKER`). The sandbox-job service account
(`themis-sandbox-job`) has no principal that calls as `CALLING_AS_SELF`; both of its principals (`CALLER_AGENT` and
`CALLER_SANDBOX_WORKER`) require a session token that resolves through the auth service, so a sandbox process that drops
its session token cannot satisfy any RPC's admission rule. Any RPC that declares no admitted principals is denied by
default, except to the developer identity `CALLER_CLU`, which is implicitly admitted to every RPC so engineers can call
any method during debugging (see [`rules.py`](../../themis/clients/auth/rules.py)).

To prevent authorization from growing into a per-user access-control system across the data plane, **what a call may see
is selected only by the Analysis it is scoped to, never by the person driving it.** A person's institutional entitlement
can widen only what is *acquired* into the corpus through `MaybeIngestPapers`. Because acquiring a subscription paper
places it into a corpus that every Project currently reads, the person axis is designed here but not yet built: nothing
reads an approval token today. Once the token is wired, a construction check will reject approval tokens whenever no
approval resolver is configured, holding the axis off until both the browser approval flow and licence-scoped corpus
masking are implemented.

## Background

A **Project** is the access boundary: one dataset and the users who work on it. An **Analysis** is a collaborative
investigation bound to one Project. Authorization scopes data-plane calls at the Analysis level because the workspace
behind an Analysis is shared among every member of the Project, so any finer-grained boundary would leak through the
shared workspace.

A **session** is a short-lived bearer token issued by the auth service that resolves to a Project ID and an Analysis ID
([`security.md`](security.md); session client at
[`themis/clients/auth/session.py`](../../themis/clients/auth/session.py)). A resolved session identifies an Analysis
scope rather than a specific caller: any first-party service account granted KMS signing permission on the session-token
key can **derive** a bearer token for any session ID. The web tier derives session tokens when creating sessions, the
dispatcher derives one when launching a sandbox, and the maintainer automation account (`themis-clu`) can be configured
to derive them in development stacks.

The **sandbox agent** (untrusted model-driven code running inside a network-isolated `bubblewrap` guest managed by
`postern`) reaches internal gRPC services only through the sandbox worker's **hatch**
([`sandbox-worker.md`](sandbox-worker.md)). The hatch dials backend services using the sandbox-job service account
(`themis-sandbox-job`) and injects the session token from the worker's environment into outgoing gRPC metadata. The
guest holds no credentials of its own, cannot mint tokens, and cannot pass its own gRPC metadata across the hatch. One
worker serves one session and holds one session token, which bounds what a compromised worker can reach in the
workspace; the corpus is shared, and one token reads it as any other does. Consequently, **the agent can neither see nor
choose whom a call speaks for**: every credential on an agent call is injected outside the guest.

Internal service-to-service calls authenticate through Cloud Run IAM. The caller sends a Google-signed OIDC ID token in
the `Authorization` header, and Cloud Run rejects the request unless the caller holds `roles/run.invoker`. After
checking that permission, **Cloud Run forwards the `Authorization` header to the container unchanged** with its
signature intact (see the [Appendix](#appendix-cloud-run-authorization-header-forwarding)). The receiving container can
therefore re-verify the ID token and read the calling service account's email.

An internal call can thus carry three distinct credentials:

| Credential           | Header / carrier                         | What it proves                                                                                                                                      |
| -------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Google OIDC ID token | `Authorization: Bearer <id-token>`       | **Reachability and caller identity**: the call came from a specific first-party service account (`themis-web`, `themis-sandbox-job`, `themis-clu`). |
| Session token        | `x-themis-claim-bin` (protobuf metadata) | **Scope**: the call is bound to a specific Project and Analysis (`SessionContext`).                                                                 |
| Approval token       | Acquisition request payload (planned)    | **Entitlement**: a named user approved fetching specific papers under their institutional subscription.                                             |

The web tier's paper-content pane already calls the literature service directly using the web tier's ID token and no
session token
([`apps/web/src/server/adapters/live/literature.ts`](../../apps/web/src/server/adapters/live/literature.ts)), which is
why browser-facing literature reads cannot use a session-only gate.

## Non-goals

- **Corpus masking.** Filtering the corpus by institutional licence or publication-date cutoff is out of scope. This doc
  defines the `LiteratureBackend.view_for` seam that receives `AuthContext` on every read; the masking logic behind that
  seam is a separate design (see [Open questions](#open-questions)).
- **The browser approval flow.** How the workbench prompts a user to approve a paper fetch, how that token reaches the
  backend, and the exact protobuf schema for a resolved user identity belong to the acquisition flow design. This doc
  specifies only the security properties the resulting token must satisfy: it must resolve server-side, be unforgeable
  by the sandbox worker that relays it, and bind the user to the specific papers they approved.
- **Deployment wiring.** The environment variables supplying the GCP project ID and the service's audience URL are
  configured in the deployment manifests, not here.

## Design

The authorization library lives alongside the session client in [`themis/clients/auth/`](../../themis/clients/auth/). It
consists of three parts: a per-call `AuthContext` built from incoming credentials, an admission rule derived from each
RPC's protobuf options, and a server-wide gRPC interceptor (`AuthInterceptor`) that constructs the context and enforces
the rule.

```
Caller (Web / Worker / Hatch / CLU)
  │
  │  Authorization: Bearer <Google ID token>
  │  x-themis-claim-bin: CallerClaim(calling_as, session_token)
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ AuthInterceptor (themis/clients/auth/interceptor.py)                    │
│                                                                         │
│  1. Verify ID token against service URL audience ──► caller email       │
│  2. Decode CallerClaim & check account consistency ──► calling_as       │
│  3. If session_token present, ResolveSession ────────► SessionContext   │
│  4. Build frozen AuthContext(caller, calling_as, session)               │
│  5. Evaluate RPC Rule derived from proto options (admits_caller)        │
└─────────────────────────────────────────────────────────────────────────┘
  │
  │  AuthContext bound to contextvars for handler lifetime
  ▼
Servicer handler / backend port (LiteratureBackend.view_for, Sheaf store)
```

### One context built for every call

For every inbound RPC (except the health check), `AuthInterceptor` builds one frozen `AuthContext` instance
([`themis/clients/auth/context.py`](../../themis/clients/auth/context.py)) containing:

- `caller: str` — the verified service-account email extracted from the Google ID token.
- `calling_as: CallingAs` — the caller's claimed mode (`CALLING_AS_SELF`, `CALLING_AS_AGENT_SESSION`, or
  `CALLING_AS_WORKER_SESSION`), sent in the `x-themis-claim-bin` metadata header (`CallerClaim` in
  [`schema/proto/themis/rpc/auth.proto`](../../schema/proto/themis/rpc/auth.proto)).
- `session: SessionContext | None` — the resolved Project and Analysis binding returned by `ResolveSession` when
  `CallerClaim` includes a session token, or `None` when the call is unscoped.

Why does the caller send a `calling_as` claim in addition to its verified service-account identity? If every service
account acted in only one role, the verified email from the ID token would suffice. That breaks for
`themis-sandbox-job`: a single sandbox container runs both the worker's own workspace synchronization (which reads and
publishes to the Sheaf repository mirror) and the hatch (which forwards the untrusted guest agent's RPCs). Both paths
authenticate to Cloud Run as `themis-sandbox-job`, yet the guest must not be allowed to call worker-only RPCs such as
`Publish`. Having the worker attach a `CallerClaim` (`CALLING_AS_AGENT_SESSION` on the hatch,
`CALLING_AS_WORKER_SESSION` on its own client) lets the interceptor distinguish the two principals sharing that service
account.

The claim is trusted rather than verified, because the account is trusted to make it; what is checked is only that the
verified account has a member in the `Caller` enum for what it claims (`account_id` + `calling_as`). A call that omits
the `x-themis-claim-bin` header defaults to `CALLING_AS_SELF` with no session. The person identity will join
`AuthContext` as a fourth field once the approval flow defines its resolved message.

### What the context guarantees at construction

Certain combinations of credentials indicate a bug or a compromised caller rather than a normal authorization denial.
`AuthContext` rejects these combinations during construction with an internal error (`INTERNAL`) instead of passing them
to the rule evaluator to fail with `PERMISSION_DENIED`:

- **A claim the caller cannot honour.** If `x-themis-claim-bin` fails protobuf decoding, if the claim specifies
  `CALLING_AS_AGENT_SESSION` or `CALLING_AS_WORKER_SESSION` without a non-empty `session_token`, or if an account with
  no `CALLING_AS_SELF` principal (such as `themis-sandbox-job`) makes a call with no claim header at all, construction
  fails immediately. Because the sandbox worker and hatch always attach a claim, an unclaimed call from
  `themis-sandbox-job` indicates a worker defect or compromise.
- **An approval token when no resolver is configured (planned).** The person axis is off today because nothing in
  [`themis/clients/auth/`](../../themis/clients/auth/) reads an approval token yet. Once the approval token is read, any
  call carrying an approval token while no approval resolver is configured will raise an error rather than silently
  ignoring the token, so enabling the person axis requires explicitly wiring the resolver after the approval flow and
  licence-scoped corpus masking are ready.

That `themis-sandbox-job` is never admitted by identity alone follows directly from the `Caller` enum definition: both
principals bound to `themis-sandbox-job` (`CALLER_AGENT` and `CALLER_SANDBOX_WORKER`) require a session claim, and a
session-claiming principal is satisfied only when its session token resolves. A test verifies that every `account_id` in
the `Caller` enum matches a service account declared in the Terraform infrastructure, preventing typos or stale account
names.

`AuthInterceptor` is the sole reader of authorization metadata. A test over the servicer modules asserts that no
servicer reads gRPC invocation metadata directly, ensuring that headers carrying identity or entitlement cannot bypass
the interceptor.

### The contract declares admission and the interceptor derives the rule

Which principals may call an RPC is declared in protobuf by the repeated `admits_caller` method option in
[`sandbox_options.proto`](../../schema/proto/themis/rpc/sandbox_options.proto). Each entry names a principal from the
closed `Caller` enum:

| `Caller` enum member    | `(account_id)` option | `(calling_as)` option       | Requires resolved session?    |
| ----------------------- | --------------------- | --------------------------- | ----------------------------- |
| `CALLER_WEB`            | `themis-web`          | `CALLING_AS_SELF`           | No (optional for attribution) |
| `CALLER_CLU`            | `themis-clu`          | `CALLING_AS_SELF`           | No (optional for attribution) |
| `CALLER_AGENT`          | `themis-sandbox-job`  | `CALLING_AS_AGENT_SESSION`  | Yes                           |
| `CALLER_SANDBOX_WORKER` | `themis-sandbox-job`  | `CALLING_AS_WORKER_SESSION` | Yes                           |

Marking an RPC `admits_caller = CALLER_AGENT` both admits `CALLER_AGENT` at the server interceptor and drives code
generation for the sandbox guest's stubs and hatch allowlist ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)). The
rule the interceptor derives ([`rules.py`](../../themis/clients/auth/rules.py)) is the disjunction over the RPC's
admitted principals: a call is admitted if its verified service account and `calling_as` claim match one of the RPC's
principals, and—when that principal claims a session—its `SessionContext` resolved. Every RPC admits `CALLER_CLU`
without declaring it, so a developer identity can reach any method during debugging (see
[`rules.py`](../../themis/clients/auth/rules.py)); whether a human can impersonate `themis-clu` in a given environment
is governed by IAM and IAP.

Because service-account email addresses include the per-environment GCP project ID (for example,
`themis-web@<project>.iam.gserviceaccount.com`), the protobuf option stores only the environment-independent
`account_id` prefix (`themis-web`), and the verifier completes the email using the project ID of the environment the
service runs in. Using a protobuf enum rather than free-form strings turns a misspelled caller into a proto compile
error and keeps principal additions backward-compatible under `buf`.

Admission is strictly an allowlist: the contract states which principals may call an RPC, and there is no option to name
a denied caller. Adding a denylist alongside the allowlist would create two places stating who may call an RPC that
could disagree with each other; if an admission rule is too broad, the fix is to narrow the allowlist.

For each call, fail-closed evaluation proceeds in two stages:

1. **Caller verification.** Before building `AuthContext`, the interceptor verifies the bearer token in `Authorization`
   against Google's public certificates with the expected audience set to the receiving service's own URL. Checking the
   audience inside the container is essential even on Cloud Run: when a service sits behind a load balancer with
   Identity-Aware Proxy (IAP), Cloud Run checks only that IAP's service agent holds `roles/run.invoker` and passes the
   caller's `Authorization` header through unexamined ([`security.md`](security.md#ingress-from-the-public-internet)).
   Verifying signature and audience ensures that neither a forged token nor a token minted for another service produces
   a `caller` email. If verification fails, the call is rejected with `UNAUTHENTICATED`.
1. **Context construction and rule evaluation.** Once the caller is verified, the interceptor decodes
   `x-themis-claim-bin` and resolves the session token if one was provided. If a call includes a session token that
   fails to resolve (even on a `CALLING_AS_SELF` call where the RPC does not require a session), the interceptor rejects
   the call with `PERMISSION_DENIED` so a handler never runs with an unattributed or misattributed session. If no
   admitted principal matches `AuthContext`, the interceptor rejects the call with `PERMISSION_DENIED`. By contrast, if
   the auth service itself is unreachable during `ResolveSession`, that RPC error propagates as an availability failure
   rather than being turned into a policy denial.

### Default-deny is enforced at the interceptor across the whole server

If authorization checks lived inside individual servicer methods, a newly added method whose author forgot the check
would run ungated for any caller with `roles/run.invoker`. Wrapping the gRPC server in `gated_server`
([`themis/clients/auth/interceptor.py`](../../themis/clients/auth/interceptor.py)) makes default-deny a property of the
server, matching the default-on session gate in the web tier ([`security.md`](security.md)).

Because a gRPC server interceptor intercepts every inbound call by its full `/package.Service/Method` path before
dispatching to any servicer, it does not maintain a hand-written list of governed services. Instead, it resolves each
method path in the generated protobuf `DescriptorPool`, derives the admission rule from the method's options, and denies
any method whose contract admits no principals (except to `CALLER_CLU`) or whose path is not in the descriptor pool.
Exactly one path is exempt: `/grpc.health.v1.Health/Check`, which Cloud Run probes without credentials. The production
data-plane servers (`evidence` and `sheaf`) are constructed through the interceptor
([`interceptor.py`](../../themis/clients/auth/interceptor.py),
[`__main__.py`](../../themis/services/evidence/__main__.py)), so admission on those services never depends on a handler
remembering to check (the `hello` smoke-test service is the sole exception, verifying its session in-handler via
`require_session`).

The interceptor wraps all four gRPC method shapes (unary-unary, unary-stream, stream-unary, and stream-stream) and is
the single place that sets non-OK gRPC status codes. For server-streaming RPCs, `AuthContext` remains bound in
`contextvars` across every iteration of the response generator, because the handler body executes lazily as responses
are yielded. Handlers behind the interceptor raise `StatusError` rather than calling `context.abort()` directly, and the
interceptor translates `StatusError` into `context.abort()`.

On client-streaming (`stream-unary`) RPCs, aborting the call while the client is still uploading chunks causes the
server to send trailers and reset the HTTP/2 stream
([RFC 9113 §8.1](https://www.rfc-editor.org/rfc/rfc9113.html#section-8.1)). While standard HTTP/2 clients preserve the
trailers' gRPC status, the `grpcio` Python `grpc.aio` client has a bug
([grpc/grpc#36066](https://github.com/grpc/grpc/issues/36066), fixed in
[grpc/grpc#43486](https://github.com/grpc/grpc/pull/43486)) where a write failing after stream reset overwrites the
server's status with `INTERNAL`. Callers of client-streaming RPCs (such as `Publish` on the Sheaf service) therefore use
the synchronous `grpcio` client until a `grpcio` release containing that fix is adopted.

### The binding names an Analysis, not a caller identity

A session token resolves to a Project ID and an Analysis ID (`SessionContext`). Any service account that can derive
session tokens—the web tier, the dispatcher, or `themis-clu`—can present a valid token for any live session.
Consequently, requiring a resolved session scopes a call to an Analysis; it does not prove that the caller is the
sandbox agent. An RPC's `admits_caller` list states which caller principals may invoke the method, and the resolved
`SessionContext` provides the Analysis scope.

### The person is resolved from an approval token, never asserted

When an acquisition call needs to draw on a human user's institutional subscription, the obvious implementation is for a
trusted caller to pass the user's email and institution in a plaintext metadata header. That breaks on the sandbox path:
agent calls are relayed by the sandbox worker, which runs alongside untrusted model-driven code and could fabricate a
plaintext header if compromised. Instead, when the user approves a fetch in the browser, the web tier mints an opaque
**approval token** that the sandbox worker relays without being able to forge, and the data plane resolves that token
through the auth service just as it resolves session tokens today. This path is not yet built: neither the auth-service
RPC nor an approval-token reader in [`themis/clients/auth/`](../../themis/clients/auth/) exists yet, so the person axis
is off today.

| Fact        | Presented on the call                                      | Resolves to                                                  |
| ----------- | ---------------------------------------------------------- | ------------------------------------------------------------ |
| Scope       | Session token (minted when work is dispatched)             | Project ID and Analysis ID (`SessionContext`)                |
| Entitlement | Approval token (minted when the user approves the request) | User identity, institution, and the specific papers approved |

Resolving the approval token server-side keeps user attributes out of the sandbox host and reuses the auth service's
token expiry and revocation model.

Because the sandbox agent chooses which papers to pass to `MaybeIngestPapers`, an approval token must be bound to the
specific papers the user approved, and that check cannot happen in the interceptor (which sees only metadata, not the
request body). The `MaybeIngestPapers` handler must compare the requested paper IDs against the approved paper IDs in
the resolved token and reject any paper outside the approval. Two constraints on the approval flow follow from this:

1. **The approval token travels in the request body, not in gRPC metadata.** The sandbox hatch fixes outgoing gRPC
   metadata once for the lifetime of a worker execution, whereas approvals are granted per acquisition request during a
   session.
1. **Each approval token is scoped to one approved batch of papers.** Acquiring additional subscription papers later in
   the same Analysis requires a new approval token.

### What a call may see vs what a call may acquire

What a call may **see** in the corpus is determined solely by the Analysis it is scoped to, never by the person driving
the call. On every corpus read, the literature servicer passes `AuthContext` to the backend port
(`LiteratureBackend.view_for` in
[`themis/services/evidence/literature/backend.py`](../../themis/services/evidence/literature/backend.py)), which returns
a `LiteratureView` scoped to the call's Analysis. Today `view_for` returns an unrestricted view of the whole corpus,
serving as the single seam where corpus masking will plug in. Selecting the corpus view by Analysis rather than by
person preserves two properties:

- **Reproducible auditing:** Two curators opening the same Analysis see the exact same evidence, so a reviewer can audit
  the agent's citations against the same corpus the agent searched.
- **No cross-user leakage within a Project:** Because the workspace behind an Analysis is shared across all Project
  members, any text pulled into the workspace by one user's view is visible to everyone else in the Project anyway.

Browser paper-content reads from the web tier (`CALLER_WEB`) currently carry no session token and therefore read the
unrestricted corpus. Once corpus masking requires an Analysis scope on browser reads, the web tier can attach a
`CALLING_AS_SELF` claim carrying the session token of the Analysis open in the workbench.

What a call may **acquire** into the corpus is the only question answered by a person's entitlement. Fetching a
closed-access paper requires a person's institutional subscription, so the person axis is used exclusively on the
acquisition path (`MaybeIngestPapers`). However, the literature corpus is shared across every Project, and
[`PRODUCT.md`](../PRODUCT.md) §7 states that licensed literature is never shared across institutional boundaries. Today,
papers uploaded through a user's institutional access are marked `SUPPLIED` and served to any Analysis and to the
browser. While occasional user-supplied uploads already cross that boundary, automating institutional acquisition via
approval tokens would make licensed papers a primary way the shared corpus grows. The person axis is therefore held off
at `AuthContext` construction until the approval flow and licence-scoped corpus masking land.

### Applying the model to the literature and Sheaf services

#### Literature interface

The literature service ([`schema/proto/themis/rpc/literature.proto`](../../schema/proto/themis/rpc/literature.proto))
applies the admission model across three groups of RPCs (11 methods total):

| RPC group                   | Methods                                                                                                                     | Declared admission                                          | Session & context usage                                                                                                                                                                                 |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent-facing reads          | `SearchEuropePmc`, `FetchPubmedArticles`, `SearchLitVar`, `ListLitVarEntities`, `PollFullTexts`, `ReadMarkdown`, `Validate` | `admits_caller = CALLER_AGENT` (plus implicit `CALLER_CLU`) | Requires resolved session for `CALLER_AGENT`; passes `AuthContext` to `LiteratureBackend.view_for`.                                                                                                     |
| Browser paper-content reads | `DescribePaper`, `ResolveContent`, `Locate`                                                                                 | `admits_caller = CALLER_WEB` (plus implicit `CALLER_CLU`)   | Admits the web tier with its ID token and no session; passes `AuthContext` to `LiteratureBackend.view_for`.                                                                                             |
| Producer / acquisition      | `MaybeIngestPapers`                                                                                                         | `admits_caller = CALLER_AGENT` (plus implicit `CALLER_CLU`) | Authorizes up front at the interceptor before inspecting the batch; passes `AuthContext` to `LiteratureBackend.view_for` so both producer actions (`ingest_papers` and `request_conversions`) share it. |

Previously, `MaybeIngestPapers` resolved a session token inside the handler only when it reached the step of enqueuing a
PDF-to-markdown conversion task, and then discarded the binding; batches whose papers were already ingested succeeded
without any token. Gating `MaybeIngestPapers` at the interceptor rejects unauthorized callers before any work runs, and
the handler passes `AuthContext` to `LiteratureBackend.view_for(auth_context.current())` before running either of its
two producer actions that spend money — `view.ingest_papers` (which mints a permanent crosswalk claim in Cloud SQL and
copies bytes into the corpus) and `view.request_conversions` (which enqueues PDF-to-markdown conversion that spends VLM
tokens). Passing `AuthContext` into `view_for` establishes the seam for attributing both producer actions to the call's
scope, although recording or charging that attribution is not yet wired (`view_for` currently discards `auth`). The
conversion task that `MaybeIngestPapers` enqueues carries no credentials of its own: naming the paper and the main-text
target in the queue is what authorizes the conversion worker to spend model budget
([`evidence-fulltext.md`](evidence-fulltext.md#naming-a-target-is-the-decision-to-spend)), and that spend is capped at
the Anthropic workspace level rather than attributed to an Analysis or Project
([`evidence-fulltext.md`](evidence-fulltext.md#the-workspace-spend-limit-caps-spend)).

#### Sheaf service

The Sheaf service ([`sheaf-service.md`](sheaf-service.md), [`sheaf.proto`](../../schema/proto/themis/rpc/sheaf.proto))
uses the same interceptor to gate its unary and streaming repository RPCs:

- **Sandbox worker (`CALLER_SANDBOX_WORKER`)**: the worker's synchronization mirror (`CALLING_AS_WORKER_SESSION`) reads
  and publishes packs, whereas `CALLER_AGENT` is admitted nowhere on `SheafService` so guest calls forwarded across the
  hatch cannot reach the repository.
- **Web tier (`CALLER_WEB`) and developer CLI (`CALLER_CLU`)**: the web tier relays browser reads and publishes curator
  edits ([`workbench-workspace.md`](workbench-workspace.md)), and only `CALLER_WEB` signs asset download URLs;
  `CALLER_CLU` is admitted implicitly as everywhere else without being declared in
  [`sheaf.proto`](../../schema/proto/themis/rpc/sheaf.proto). Both `CALLER_WEB` and `CALLER_CLU` attach a
  `CALLING_AS_SELF` claim carrying the target session token; although `CALLING_AS_SELF` does not require a session for
  admission, the interceptor resolves the attached session token so the Sheaf servicer
  ([`servicer.py`](../../themis/services/sheaf/servicer.py)) can read the target Analysis from `AuthContext.session`
  without containing any session-resolution code of its own.

## Alternatives considered

- **Keep the boolean session gate and special-case the web tier.** Adding per-RPC exceptions for the web tier violates
  the single default-deny chokepoint rule in [`security.md`](security.md) and still discards the resolved Analysis
  binding needed for corpus view selection and repository scoping.
- **Represent the caller as a mutually exclusive union (`Session | ServiceAccount`).** Every internal call arrives with
  a verified service-account identity, and an approved acquisition call carries a service account, an Analysis session,
  and a human user's approval simultaneously. A tagged union would discard the service-account identity on
  session-scoped calls and could not represent calls that carry both a session and a person.
- **Define `AuthContext` as a protobuf message.** In generated Python protobuf code, message fields cannot be truly
  absent (`HasField` is easy to forget, and accessing an unset submessage returns a default empty instance), so an
  unscoped call could accidentally be treated as scoped to an empty `Analysis` ID. Protobuf messages are also mutable
  and provide no constructor hook for enforcing invariants. Because `AuthContext` never crosses a process boundary, a
  frozen Python dataclass is safer and simpler.
- **Declare abstract roles in protobuf and map roles to service accounts in deployment config.** Indirecting through
  role names adds a per-environment configuration map, requires startup checks to catch unconfigured roles, and moves
  the guarantee that `themis-sandbox-job` never acts without a session out of the type definition and into deployment
  config. Because the `account_id` prefix of our first-party service accounts is identical across environments and there
  are only a handful of callers per RPC, naming principals directly in the `Caller` enum is clearer.
  Environment-specific access restrictions belong in Cloud Run IAM (`roles/run.invoker`) and IAP.
- **Declare admission with Python decorators on servicer methods (`@admits(...)`).** Putting admission rules on Python
  methods duplicates information that `admits_caller` already places in `.proto` files, requires tests to keep the
  decorators and proto options in sync, and makes default-deny depend on authors remembering to decorate every method.
- **Omit explicit admission declarations and let each handler check the context fields it uses.** Read handlers have no
  natural reason to inspect `AuthContext` once they pass it to the backend port, so any read handler without an explicit
  check would remain open to every holder of `roles/run.invoker`.
- **Use separate boolean `agent_exposed` and `admits_session` proto options instead of unified `Caller` principals.** A
  session-scoped call is always made by a specific service account claiming a session (`CALLER_AGENT` or
  `CALLER_SANDBOX_WORKER`). Folding session requirements and guest-stub exposure into the `Caller` principals on
  `admits_caller` states in a single option *which* service account may call the RPC, *whether* it must present a valid
  session, and *whether* the RPC is exposed to the sandbox guest (`CALLER_AGENT`) or restricted to the worker host
  (`CALLER_SANDBOX_WORKER`, such as Sheaf's `Publish`).
- **Give the sandbox guest a user-scoped credential.** The sandbox agent executes model-generated code over untrusted
  text retrieved from the web. Placing a curator's personal credential inside the sandbox would allow prompt-injected
  code to act as that curator across every Project they belong to. Relaying a per-request approval token minted by the
  web tier limits the agent to the specific papers the user approved.
- **Have the sandbox worker assert the Analysis ID alongside the session token.** Rejected for now, and not because the
  worker is untrusted: it already chooses which session token to inject, so it is already an authority on scope. Rather,
  the sandbox worker intentionally never learns the Analysis ID; it holds only the opaque session token, and services
  such as Sheaf resolve the token to scope workspace access ([`sandbox-worker.md`](sandbox-worker.md)). Having the
  worker pass an explicit Analysis ID would expose extra metadata to the sandbox host only to save a fast auth-service
  lookup, while introducing two sources of truth for the call's scope.

## Open questions

- **Whose institutional licence governs a shared corpus view?** [`PRODUCT.md`](../PRODUCT.md) §7 states that opening a
  paper "follows the user's institutional licensing". Reconciling that rule with an Analysis-scoped corpus view requires
  scoping access per *representation* rather than per paper: extracted facts and **knowledge units** (structured claims
  extracted from a paper) can be pooled across the corpus, while licensed full text is restricted by licence. The open
  policy question is whether an Analysis's full-text view is governed by the reading user's own institution (matching §7
  literally, but breaking the invariant that two curators in a multi-institution Project see identical full text) or by
  the union of the Project members' institutions (which shares one institution's text with collaborators from another
  institution and requires an explicit exception to §7). In either case, the corpus must record which user and
  institution acquired each licensed representation, and `LiteratureBackend.view_for` must push the licence predicate
  into the search index query rather than filtering results post-hoc (which would break pagination and leak filtered hit
  counts).
- **Running a deployed dev probe to verify Cloud Run header forwarding.** Although Cloud Run's `Authorization` header
  forwarding is documented (see [Appendix](#appendix-cloud-run-authorization-header-forwarding)) and covered by
  unit-test fixtures, a probe against a deployed dev service should confirm end-to-end token verification before
  production traffic relies on it.

## Appendix: Cloud Run `Authorization` header forwarding

Caller verification in `AuthInterceptor` relies on Cloud Run forwarding the caller's `Authorization: Bearer <ID token>`
header to the container unchanged after validating `roles/run.invoker`. Google's
[service-to-service authentication documentation](https://cloud.google.com/run/docs/authenticating/service-to-service)
confirms this behaviour in two places:

1. The receiving-service code samples read the bearer token directly from the incoming `Authorization` header and verify
   its cryptographic signature against Google's public certificates, which requires the original signed token to reach
   the container unmodified.
1. The documentation for the alternative `X-Serverless-Authorization` header (used when an application passes its own
   credential in `Authorization`) notes that Cloud Run strips the signature from `X-Serverless-Authorization` before
   forwarding—a distinction that exists because the standard `Authorization` header is forwarded intact.

The canonical server-side check (`cloud_run_caller_verifier` in
[`themis/clients/auth/caller.py`](../../themis/clients/auth/caller.py)) therefore re-verifies the token in
`Authorization` against Google's public certs with the audience set to the receiving service's URL, extracts the `email`
claim, and fails closed if the token is missing, invalid, or lacks a verified email.
