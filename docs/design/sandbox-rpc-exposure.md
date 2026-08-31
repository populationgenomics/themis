# Design: sandbox RPC exposure — one proto option, generated forwarding

**Status:** draft **Related:** [`proto.md`](proto.md) (the proto-is-source-of-truth codegen pipeline this extends),
[`services.md`](services.md) (the service pattern whose RPCs get exposed), [`sandbox-worker.md`](sandbox-worker.md) (the
hatch the guest reaches services through), [`security.md`](security.md) (the trust boundary).

## Purpose

Adding an agent-reachable RPC currently means correlated hand-edits in several places — the hatch allowlist
(`GUEST_METHODS`), the forwarder + `build_hatch` wiring, the guest stub accessor (`guest/services.py`), the Dockerfile's
choice of which stubs ship into the guest rootfs, and the agent prompt that documents the callable surface. Forgetting
any one is a silent bug, and the set grows as services land past the `hello` smoke test. Make the `.proto` the single
source of truth: one file option marks a file's services agent-exposed, and `regen` emits every correlated artifact from
it, so they cannot drift.

Two words carry two distinct meanings here. *Agent-exposed* is the exposure concept — the untrusted agent may call the
RPC — and is the repo-wide vocabulary (`services.md` speaks of agent-facing services). *Guest* names the
postern-sandboxed process the hatch serves those calls to; guest-side artifacts (the rootfs, `guest/services.py`) keep
that name because they are about that process, not about who may call.

## Mechanism

A custom file option is the sole authoring point — placed on any proto file that defines agent-facing services. There is
no single central file: each service-defining `.proto` either carries the option or does not, and exposure is decided
per file.

```proto
import "themis/rpc/sandbox_options.proto";
option (themis.rpc.agent_exposed) = true;

service Hello {
  rpc SayHello(greeting.SayHelloRequest) returns (greeting.SayHelloResponse);
}
```

`sandbox_options.proto` declares `extend google.protobuf.FileOptions { bool agent_exposed = <field#>; }`. The option is
in the `FileDescriptorSet`, so `regen` reads it at build time (`buf build -o`) and absent-means-false — the default is
fail-closed. Exposure is file-wide: every service in an `agent_exposed` file is agent-callable, with no per-method or
per-service carve-out — a subset is a narrower agent-facing file.

`regen` collects the services of the annotated files from the descriptor set and emits, all committed and
`regen-is-fresh`-gated:

| Artifact                                      | Derived as                                                                                                                   |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Hatch allowlist (`GUEST_METHODS`)             | every `/pkg.Service/Method` of every service in an `agent_exposed` file                                                      |
| Per-service forwarders + `build_hatch` wiring | one generated servicer per exposed service                                                                                   |
| Guest stub accessors (`guest/services.py`)    | one accessor per exposed service                                                                                             |
| Guest-stub manifest                           | the transitive import closure of the `agent_exposed` files (their `_pb2_grpc` + the closure's `_pb2`) the Dockerfile `COPY`s |
| Agent prompt fragment                         | Markdown documenting each exposed call, from the proto's leading comments (`SourceCodeInfo`)                                 |

Generation is correct by construction; a CI verification of a hand-authored allowlist would be heavyweight and not carry
that guarantee, so the allowlist generates too.

The prompt fragment closes a drift the other four don't: the agent's prompt describing the sandbox call surface must
match the exposed set, or the model is told about calls it cannot make (or not told about ones it can). Both the
callable stubs and their documentation derive from the one annotated source, so the prompt cannot describe an unexposed
call.

### Stub and message boundaries

`protoc` generates a whole service interface into a stub — it cannot emit a subset of a service's methods — so an
`agent_exposed` file's generated `_pb2`/`_pb2_grpc` are the exact agent-callable surface for its services. What ships
into the guest rootfs is the **transitive import closure** of the agent-exposed files: their `_pb2_grpc`, plus the
`_pb2` of every file they reach. The message set that must ship is already fixed by the exposed RPCs' signatures (their
request/response types and everything those reference), so inferring it from the import closure ships exactly what is
required — hand-marking each message file would be redundant and could omit a needed one.

The one validation `regen` enforces, reading the descriptor set: **no service defined in a non-`agent_exposed` file may
appear in an `agent_exposed` file's transitive closure** — every service reachable from an agent-facing file is itself
agent-facing. Messages from unmarked files are fine (they are inert data, and their shipping is forced by the contract).
So a message shared between an exposed and an unexposed service needs no marker — it is inferred-visible by being
imported — while an internal *service* can never leak in: if an agent-facing file's closure reached one, `regen` fails
and the mixed file is split. Gating on services, not messages, targets the only thing that is a callable surface.

Defining distinct agent-facing messages does not reintroduce forwarder translation: the backend a forwarder targets
implements the agent-facing proto directly (as `themis-hello` implements `Hello`), so the agent-facing contract is the
wire contract end to end and the forwarder stays a pure pass-through.

### Forwarder model

One `GrpcHatch` serves one UDS, hosting N servicers — `add_servicer` once per exposed service. gRPC dispatches by method
to the right servicer, so each generated forwarder holds only *its* service's upstream channel; per-service endpoint
routing falls out for free. The forwarder body is boilerplate — inject the session token, forward — because policy
(scoping, size caps, authorization) lives in the service behind the network hop, reached with the injected session
identity; duplicating it in the shim would only add a second copy to drift. Codegen emits the body per RPC cardinality
(unary / server-stream / client-stream / bidi) from the descriptor.

The upstream channel per exposed service is the one thing the proto cannot carry — it is worker deploy config. The
worker holds a `service → channel` map and fails loud at startup if an exposed service has no channel.

A forwarded call is bounded by the caller's own remaining time, capped below the harness's per-shell-call limit. The cap
is what matters: the hatch server is synchronous, so nothing cancels a forwarded call when the guest that asked for it
exits, and an answer that arrives after the guest is gone is a serving thread and an upstream instance held for nothing.
Because anything in the guest can dial the hatch and name its own deadline, that bound cannot be a default a caller
overrides.

A forwarded failure crosses as its status code; its text crosses only where the upstream servicer wrote it. The
forwarder cannot ask who wrote a message, so it goes by the code: those the evidence contract maps a domain error onto
carry the servicer's own words, which the agent needs in order to correct its request. Under any other code the words
are gRPC's, or the infrastructure's in between — and a channel-level failure's name the upstream it resolved — so the
guest gets the code alone.

A generic byte-forwarding proxy (zero generated forwarder code) was considered and rejected — see Alternatives.

## In practice: adding an agent-exposed service

Building on `services.md`'s checklist for a service, exposing it will be one authoring edit plus one deploy-config entry
once the generator above is whole. Today the correlated artifacts are still hand-written, and the checks under
Implementation state are what tell you which of them you owe:

1. **Satisfy the condition first** — read Security. Marking a file is the assertion that every RPC in it survives a
   hostile caller; the rest of this list is mechanical by comparison.
1. **Proto** — mark the service's `.proto` file `option (themis.rpc.agent_exposed) = true;` (import
   `themis/rpc/sandbox_options.proto`). Every service in that file is now agent-callable; split the file if some
   services must stay worker-only.
1. **`regen`** — regenerates the hatch allowlist. Commit the diff; `regen-is-fresh` gates it.
1. **The artifacts the allowlist now outruns** — a forwarder and its `build_hatch` wiring, a `guest/services.py`
   accessor, and the guest rootfs stub selection. Run the sandbox-worker tests: with the allowlist grown and none of
   these written, each missing one is named by a failure.
1. **Worker deploy config** — add the service's upstream channel to the worker's `service → channel` map. A missing
   entry for an exposed service fails the worker loudly at startup.

## Security

Exposure is a security assertion — "this RPC is safe against an adversarial caller" — not a convenience. The forwarder
is cheap; the exposed *service* is what carries the risk: the untrusted agent can now invoke that RPC as the session, so
every exposed RPC must be written to assume a hostile caller.

The condition a file has to meet, and go on meeting, is that **every RPC it defines admits only a verified session
before it does any of the caller's work, and nothing it then does on the caller's behalf is unbounded in cost.** The
token is not always a scope — the evidence corpus is public, so there the check is authorization alone — but it is
always the gate. `store` and `auth` fail the condition plainly: they are not written against a hostile caller at all.

`literature` fails it for a sharper reason worth naming, because its proto and servicer sit in the same deployment the
hatch already dials, so nothing but the condition keeps it out. Most of its RPCs resolve no session at all. The one that
does, `MaybeIngestPapers`, resolves one only at its enqueue step — by which point it has already run the caller's
crosswalk lookups and readiness reads. That placement is right for a trusted caller, where the question is who pays for
the conversion; it is the wrong shape for a hostile one, who is answered work for free and needs no enqueue to be worth
their while. What would qualify the file is its RPCs coming to gate at the door rather than at the expensive step — not
a rework landing.

Sharing an RPC with a trusted caller is fine while its *response* does not differ by caller trust; the evidence RPCs
answer the web tier's backend and the agent identically. What is barred is an RPC whose response would have to be
filtered per caller — that pushes policy back into the forwarder, which is the one place it must not live. Where the
agent needs a narrower view of something a trusted caller also reads, that is a distinct agent-facing RPC.

The ceremony that keeps the option from being a rubber-stamp:

- **Fail-closed default.** No option ⇒ not exposed. Absence is a definite "no", not a "forgot".
- **A schema-only edit cannot land an exposure.** Marking a file grows the generated allowlist, and three committed
  checks then fail until a forwarder, a guest stub accessor and a shipped stub exist for it — the allowlist is asserted
  *equal* to the set of services with forwarders, not merely covered by it. So exposure still takes a deliberate second
  edit by someone who sees the service, which is the property that matters. The intended form of this is a co-signature
  the service owner writes rather than a test they satisfy: an `@agent_exposed` decorator on the hand-written servicer
  class that owns the hostile-caller hardening, cross-checked against the descriptor in CI.
- **Visible allowlist diff.** The generated `GUEST_METHODS` is committed, so widening the boundary shows up in review.

## Alternatives considered

- **Comments/tags on the RPC, not an option.** Rejected: comments are stripped from the descriptor, invisible to
  `buf lint`/`buf breaking`, need fragile source-scraping, and cannot express a fail-closed default (a missing comment
  is indistinguishable from a forgotten one).
- **Generic byte-forwarding proxy** (one handler, no per-service forwarder code). Rejected: postern's `GrpcHatch`
  exposes only concrete-servicer registration (`add_servicer`), so this needs a postern API addition; and a generic
  handler still needs the descriptor to pick each method's cardinality, plus hand-rolled method→channel routing.
  Per-service generated forwarders use the existing API and inherit typing and cardinality from the generated servicer
  base.
- **Hand-authored allowlist + CI verification against the proto.** Rejected: heavyweight and not correct by
  construction, where generation is. The deliberate-review property it aimed for is supplied instead by the decorator
  co-signature and the committed allowlist diff.
- **Runtime registry as the source of truth** (derive the allowlist from registered forwarders). Rejected as the *root*:
  the guest process cannot import the worker's registry, and the Dockerfile runs before any worker exists, so the
  guest-stub and image-build artifacts need a source readable statically at build time — which the proto option is.
- **Per-method or per-service exposure** (a `MethodOptions`/`ServiceOptions` flag). Rejected: `protoc` cannot subset a
  service, and a `_pb2_grpc` covers a whole file, so anything finer than file-wide would ship worker-only methods or
  services into the guest stub (refused at the hatch, but leaking their names and shapes). A file-level flag makes the
  clean stub free — the shipped file is the exact surface — and needs only the one closure validation; a finer flag
  would need an extra no-mixing lint on top. A subset is a narrower agent-facing file.
- **Explicitly marking every guest-visible file** (including message-only files). Rejected in favor of inferring the
  shipped set from the agent-exposed files' import closure: the message set is already fixed by the exposed RPC
  signatures, so explicit marking is redundant and can omit a needed file. Only files that *define* agent-facing
  services are marked; the closure validation (no unmarked service reachable) is what keeps the callable boundary tight.
- **Closure check as a `buf lint` rule.** Rejected: this repo's `buf lint` rule set cannot express a cross-file closure
  rule, and a custom lint plugin would cost a pinned protoplugin binary. `regen` already reads the descriptor set to
  emit the artifacts, so the check lives there — `regen-is-fresh` gates it in CI.

## Implementation state

The generator above is the target, not yet the whole of what runs. Shipped: the option, `regen`'s descriptor read, and
the generated `GUEST_METHODS` the hatch consumes.

Hand-authored, each held to the exposed set by a check rather than by generation — the per-service forwarders and
`build_hatch`'s wiring, and the guest stub accessors, by
[`test_hatch.py`](../../themis/services/sandbox_worker/tests/test_hatch.py) (descriptor-derived and closed-world, one
case per rpc, plus a dial of every allowlisted method against a live hatch); the Dockerfile's stub selection, by
[`test_guest_rootfs.py`](../../themis/services/sandbox_worker/tests/test_guest_rootfs.py) (a stub per exposed service,
and the import closure of everything the stage lands).

Not built: the guest-stub manifest, the agent prompt fragment, the `@agent_exposed` decorator and its
descriptor⟺decorator cross-check, the closure validation, and the `CLAUDE.md` directive that would make an agent
proposing an exposure argue the condition above unprompted. So the prompt's account of the callable surface is still
hand-maintained, and the drift the fragment exists to close stays open.

## Open questions

- The decorator's name and module, and where the cross-check test imports the servicers from.
- The manifest format the Dockerfile consumes.
