# Design: sandbox RPC exposure — one proto option, generated forwarding

**Status:** draft **Related:** [`proto.md`](proto.md) (the proto-is-source-of-truth codegen pipeline this extends),
[`services.md`](services.md) (the service pattern whose RPCs get exposed),
[`../plans/postern-sandbox-swap.md`](../plans/postern-sandbox-swap.md) (the hatch the guest reaches services through),
[`security.md`](security.md) (the trust boundary).

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

A generic byte-forwarding proxy (zero generated forwarder code) was considered and rejected — see Alternatives.

## In practice: adding an agent-exposed service

Building on `services.md`'s checklist for a service, exposing it to the sandbox agent is two authoring edits plus one
deploy-config entry; `regen` produces everything else:

1. **Proto** — mark the service's `.proto` file `option (themis.rpc.agent_exposed) = true;` (import
   `themis/rpc/sandbox_options.proto`). Every service in that file is now agent-callable; split the file if some
   services must stay worker-only.
1. **Servicer** — carry the `@agent_exposed` decorator on the hand-written servicer class (the co-signature CI
   cross-checks against the descriptor; see Security).
1. **`regen`** — regenerates the hatch allowlist, per-service forwarders + `build_hatch` wiring, `guest/services.py`
   accessors, the guest-stub manifest, and the agent prompt fragment. Commit the diff; `regen-is-fresh` gates it.
1. **Worker deploy config** — add the service's upstream channel to the worker's `service → channel` map. A missing
   entry for an exposed service fails the worker loudly at startup.

Nothing about the hatch, the Dockerfile stub selection, or the prompt is hand-edited — the two annotations and the
channel entry are the whole surface.

## Security

Exposure is a security assertion — "this RPC is safe against an adversarial caller" — not a convenience. The forwarder
is cheap; the exposed *service* is what carries the risk: the untrusted agent can now invoke that RPC as the session, so
every exposed RPC must be written to assume a hostile caller. `store` and `auth` stay worker-only because they are not
hardened for that, not because forwarding them is hard. Never share one RPC across the trust boundary (a shared RPC that
returns trusted-only fields would force response-filtering back into the forwarder) — design distinct agent-facing RPCs.

The ceremony that keeps the option from being a rubber-stamp:

- **Fail-closed default.** No option ⇒ not exposed. Absence is a definite "no", not a "forgot".
- **Decorator co-signature.** The hand-written backend servicer *class* implementing an agent-exposed service must carry
  an `@agent_exposed` decorator (the forwarder is generated, so the co-signature belongs on the impl that owns the
  hostile-caller hardening, not on the shim). CI cross-references the descriptor against the imported servicers and
  fails if a service in an `agent_exposed` file lacks the decorated class, or a decorated class has no matching exposed
  service. Exposure therefore takes a deliberate edit in two independent places — the proto file and the service code —
  so it cannot happen by a schema-only edit the service owner never sees. The decorator's value is the forced
  acknowledgement and the second agreeing place, not runtime behavior.
- **Visible allowlist diff.** The generated `GUEST_METHODS` is committed, so widening the boundary shows up in review.
- **`CLAUDE.md` rule.** A working directive stating the threat model — exposing an RPC hands it to untrusted code; the
  service must defend itself — so an agent proposing an exposure states the justification unprompted.

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

Not yet shipped; #235 tracks the implementation, sliced:

- **Allowlist generation** (#269, in review) — the `agent_exposed` option, the `regen` descriptor read, and the
  generated `GUEST_METHODS` the hatch consumes.
- **Planned** — per-service forwarders + `build_hatch` wiring; `guest/services.py` accessors + guest-stub manifest + the
  Dockerfile stub selection; the `@agent_exposed` decorator + descriptor⟺decorator cross-check + the `PERMISSION_DENIED`
  boundary test + the closure validation; the agent prompt fragment.

## Open questions

- The `agent_exposed` extension field number (pick and document one in the option range).
- The decorator's name and module, and where the cross-check test imports the servicers from.
- The manifest format the Dockerfile consumes.
