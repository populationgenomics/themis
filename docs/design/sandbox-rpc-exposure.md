# Design: sandbox RPC exposure — the rpc is the unit, and a session is context

**Status:** current **Related:** [`sandbox-worker.md`](sandbox-worker.md) (the hatch the guest reaches services through,
and the worker that forwards for it), [`services.md`](services.md) (the service pattern whose rpcs get exposed),
[`security.md`](security.md) (the trust boundary, and the exfiltration criterion an outbound call is held to),
[`proto.md`](proto.md) (the proto-is-source-of-truth codegen pipeline this extends),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the first service the agent and the web tier both call).

## Overview

The sandboxed agent reaches Themis's internal services through one exit, the hatch, which admits a fixed set of rpcs and
nothing else. This doc decides how that set is declared, what an rpc in it has to be, and what a session means to a
data-plane rpc.

- **An rpc is agent-callable because its own declaration says so.** One method option, `agent_exposed`, on the rpc.
  `regen` derives the hatch's allowlist, the contract tree the guest reads, the stubs generated from that tree and the
  accessor the agent calls a service through, all from that option, so none of them is authored and none can drift; what
  the option does not generate, the forwarder and the agent's instructions, is held to the allowlist by checks. Absent
  the option the rpc is unreachable: the default is fail-closed.
- **Exposure is a security assertion about the rpc, not a convenience.** Marking an rpc asserts it is written for a
  hostile caller: nothing it does on the caller's behalf is unbounded in cost, and every outbound request carrying the
  caller's text is destination-fixed and query-only.
- **A session is context an rpc uses, never a ticket it inspects and discards.** An rpc resolves the caller's session
  only where its answer depends on it — the Project's scope for project-bound data, a data cutoff for masking, the
  Analysis for attributing spend — and fails loud without one. An rpc whose answer does not depend on the session does
  not ask for one. The first intended use is masking: an evaluation session carries a data cutoff, every time-varying
  source answers as of that date, and every computational tool runs at the version pinned with it.
- **Callers differ; the rpc does not.** The agent's calls carry a session by construction, injected by the trusted
  worker. The web tier's backend presents a session only to an rpc that takes one. An rpc both of them call answers both
  identically.

## Background

**The sandbox and its one exit.** The agent's code runs in the *guest*, a sandboxed process with no network and no
credential ([`sandbox-worker.md`](sandbox-worker.md)). Its one exit is the *hatch*: a gRPC server on a Unix socket
inside the sandbox, served by the trusted *worker* process outside it. The worker registers one *forwarder* per exposed
service. A forwarder injects the session's token as request metadata and forwards the call over a channel carrying the
worker's own service identity, so the guest names a method and the trusted side decides what that means. The hatch
admits exactly the methods on a generated allowlist and answers everything else `PERMISSION_DENIED`.

**Two callers.** Two things call a data-plane service. The agent, through the hatch, whose code is model-written and
shaped by whatever it has read: papers, records, the case. It is the untrusted caller, and everything below about
hostility is about it. And the web tier's backend, the BFF, which calls a service directly on its own IAM identity to
serve the browser. It is a trusted first-party service. Reaching a service at all takes an IAM grant, which those two
callers' identities hold.

**Sessions.** An Analysis ([`../../GLOSSARY.md`](../../GLOSSARY.md)) runs as a *session*. A session has a token, minted
when the Analysis is created, that the auth service resolves to the Project and Analysis it belongs to
([`themis/clients/auth/session.py`](../../themis/clients/auth/session.py) is the servicer-side helper that does the
resolving). Only trusted processes ever hold a token: the worker holds the one for the session it runs, and the BFF can
derive the one for any Analysis it serves. The guest never sees a token, so it cannot forge one, replay one or choose
one.

**What protoc can generate.** A generated stub covers a whole service; protoc cannot emit a subset of a service's
methods. It does emit exactly what it is given, so a subset of a service is a subset of its source: a callable surface
finer than a service is made by cutting the contract before protoc sees it.

**Where the agent's text can go.** The guest has no network, so an exposed rpc that reaches a third party is the only
way anything the agent composes leaves the perimeter. Whether such an rpc is an exfiltration channel is a property of
the rpc's own code, and the criterion for it — the destination fixed by constants, the caller's text an encoded
parameter that selects a response and never names a host or a route or content the upstream stores — is
[`security.md`](security.md) §What counts as an exfiltration channel. This doc applies that criterion; it does not
restate it.

## Non-goals

- **Filtering a response per caller.** An rpc answers the agent and the BFF identically. Where the agent needs a
  narrower view of something a trusted caller reads, that is a distinct rpc. Per-caller policy would otherwise land in
  the forwarder, which is pass-through boilerplate and the one place policy must not live.
- **Deciding who may open a licensed paper.** Whether a curator may read a paper's full text follows their institutional
  licensing, an axis bound to the user rather than to the Project or the session ([`../PRODUCT.md`](../PRODUCT.md) §7).
  The user is known to the BFF alone, so that check belongs on the BFF's leg to the literature service and is the
  literature layer's to design. Nothing here stands in for it, and nothing here forecloses it.

## Design

### The rpc is the unit of exposure

The `agent_exposed` option is a method option, placed on each rpc the agent may call. It lives in
[`sandbox_options.proto`](../../schema/proto/themis/rpc/sandbox_options.proto), which every service proto that exposes
an rpc imports.

```proto
import "themis/rpc/sandbox_options.proto";

service Literature {
  // The paper's markdown rendering, for the agent to read.
  rpc GetMarkdown(GetMarkdownRequest) returns (GetMarkdownResponse) {
    option (themis.rpc.agent_exposed) = true;
  }
  // The storage object a rendering lives in, for the BFF to serve. Not exposed: nothing in the
  // guest can follow a location.
  rpc ResolveContent(ResolveContentRequest) returns (ContentLocation);
}
```

The option is in the descriptor set, so `regen` reads it at build time and absent means false. A file, a service or a
method without it contributes nothing to the callable surface.

**Why the rpc and not the file.** Callers cross-cut a service. The literature service is one domain — papers, their
renderings, the quotes cited from them — and eleven rpcs, of which three serve the browser alone: a paper's renderings
and files for the pane, the storage location of its content for the BFF to presign, the position of a quote for the
highlighter. A mark on the file exposes all eleven, so the agent is offered a location it cannot follow and has no
business seeing; the alternative the file-level design offered was to split the file along the caller axis, carving a
coherent service by who calls it. The mark says something about an rpc — that the agent may reach it, and that it is
written to be reached by the agent — and a fact about an rpc belongs on the rpc. That is also where the hardening
argument lives: an rpc's caps and its fixed destinations are its own, so the change that marks an rpc argues the
condition for that rpc.

**What `regen` derives**, all committed and held fresh by CI:

| Artifact            | Derived as                                                                                                                                                                                                |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hatch allowlist     | the `/package.Service/Method` path of every marked rpc                                                                                                                                                    |
| Guest contract tree | the exposed contract sources with everything the agent cannot reach cut out — the unmarked rpcs, the types no kept rpc reaches, the marks themselves and their import; upstream schema copies stay intact |
| Guest stubs         | protoc's own output over the guest contract tree, so a stub offers exactly the marked rpcs                                                                                                                |
| Guest accessors     | one per service with a marked rpc, handing out that stub over the one hatch channel                                                                                                                       |

Two things stay hand-written by design and are held to the allowlist by checks rather than generated: the forwarder per
service, which is the deliberate second edit the ceremony below relies on, and the agent's instructions, whose every
named rpc a test holds to the allowlist, so the model is never told about a call it cannot make.

**The guest contract tree is what makes the finer unit affordable.** Protoc emits a stub for whatever service it is
given, so rather than generating over the full contract and hiding part of the result, `regen` first derives the
contract as the guest should see it and lets protoc generate from that. The cuts are made in the source text at
positions the descriptor's own source info supplies — an rpc's span, the length of its leading comment, a type's span —
not by parsing the grammar; the one edit beyond deletion collapses a body a cut emptied. The result is recompiled and
every kept declaration's comments are held equal to the full contract's, so a wrong cut fails the build instead of
shipping. The tree is one artifact with two readers: the model reads its `.proto` files, which are the contract it can
call and nothing else, and the code it calls is generated from those same files, so the two cannot disagree. Exposure
leaves no trace inside the sandbox — no marks, no option import — because from inside, the contract simply is what is
there. The boundary stays where it was: a guest that hand-rolls a call to an unlisted method is answered
`PERMISSION_DENIED` at the hatch. The tree is discoverability for the model; the hatch is the control.

**What ships into the guest** is the tree and its generated code: the filtered sources at a fixed path the model reads,
and the stubs and messages generated from them on the import path. Because unreachable types are cut, a file imported
only for its messages ships only the messages a kept rpc reaches, and no internal service's stub ever enters the rootfs.
A type a kept rpc reaches ships whole — the cut removes declarations, never fields — so a reply from the full service
parses unchanged.

**The ceremony that keeps the option from being a rubber-stamp:**

- **Fail-closed default.** No option means not exposed. Absence is a definite no, not a forgot.
- **A schema-only edit cannot land an exposure.** Marking an rpc grows the generated allowlist and the guest contract
  tree beside it, and two committed checks then fail until a forwarder and a shipped stub exist for its service: the
  allowlist is held *equal* to the set of services with forwarders, and the rootfs is held equal to the tree
  ([`test_hatch.py`](../../themis/services/sandbox_worker/tests/test_hatch.py),
  [`test_guest_rootfs.py`](../../themis/services/sandbox_worker/tests/test_guest_rootfs.py)). So exposure still takes a
  deliberate second edit by someone who sees the service. The intended form of that edit is a co-signature the service
  owner writes: an `@agent_exposed` decorator on the hand-written servicer *method* that owns the hardening,
  cross-checked against the descriptor in CI, so the assertion sits on the code it is about.
- **Visible allowlist diff.** The generated allowlist is committed, so widening the boundary shows up in review as a
  list of method paths.

### Exposure is a security assertion about the rpc

Exposure means the untrusted agent can invoke the rpc, and the agent's requests are shaped by content nobody vetted: the
papers it read, the records it fetched, the case it was given ([`../PRODUCT.md`](../PRODUCT.md) §9 treats sources as
untrusted content). So every marked rpc is written to assume a hostile caller. The condition it meets, and goes on
meeting, is that **nothing it does on the caller's behalf is unbounded in cost, and every outbound request it makes
carrying the caller's text is destination-fixed and query-only.**

Cost has two parts, and a marked rpc has to bound both. What one call can start — a batch size, a result budget, a read
budget — is the rpc's own cap. What a stream of calls can accumulate — conversions queued, upstreams hammered — is paced
by the deployment behind the rpc, a queue's concurrency or a client's rate. The change that marks an rpc names both
bounds for it; a cap alone leaves the pace to the caller, and a pace alone leaves the first call unbounded.

The destination clause is where the agent's one path to a third party is decided, and it sits on the rpc rather than at
the hatch because the forwarder cannot check it: whether a URL is built from constants or from data is a property of the
rpc's code. An rpc whose reach is a URL built from fetched data — a host read out of a record, a link followed out of a
result, a redirect chased — does not qualify, and the shapes that violate the criterion and the residuals it leaves are
argued once, in [`security.md`](security.md).

Two services fail the condition plainly and carry no marks: `store` and `auth` are not written against a hostile caller
at all.

### A session is context, not a ticket

Consider what a session check at an exposed rpc could guard. The guest cannot present a token the worker did not inject,
so the check cannot catch a forged or borrowed session. IAM already bounds who reaches the service, so it cannot catch
an outsider. A check that verifies the token and then discards what it resolved protects nothing, and reads to the next
maintainer as a check that forgot what it was for. Worse, stated as a property of the rpc it reaches callers it was
never about: a trusted caller sharing the rpc is made to carry a credential the rpc has no use for, which is how the web
tier came to derive session tokens for browser reads of a shared corpus.

**Decision.** An rpc resolves the caller's session where its answer depends on it, and fails loud without one. Nowhere
else does an rpc ask. Three things an answer can depend on:

- **Scope.** Project-bound data — case genomes, phenotypes, anything under a Project's boundary — is read within the
  session's Project and no wider ([`../PRODUCT.md`](../PRODUCT.md) §7, cross-Project default-deny). The scope is derived
  from the token server-side, never from an argument the caller chose.
- **Cutoff.** A time-varying source answers as of the session's data cutoff, below.
- **Attribution.** An rpc that starts paid work — a full-text conversion, a fan-out to a metered upstream — records
  which Analysis asked, so spend is answerable per Analysis.

Fail loud is the load-bearing half. An answer that should have been scoped, masked or attributed and was not is a wrong
answer that looks right, which is exactly the failure the fail-loud rule exists for
([`../style/general.md`](../style/general.md)). So an rpc that takes context refuses a call that carries no session, and
the refusal is a status the caller sees, not a default the rpc falls back to.

**Where the requirement is declared.** On the rpc's declaration comment, for the caller: what the session is used for,
and that a call without one is refused. It is not an annotation. An annotation enforces nothing; its one value is
letting a descriptor-driven test detect a missing check, and that pays once a service has rpcs that resolve a session
for a use beside rpcs that do not. Literature meets that the day its cutoff lands — the eight the agent calls will take
it, the browser's three will not — and the option comes with that change, not with the marks; until then literature
resolves a session only where it enqueues a conversion, and the nine other evidence services gate every rpc alike.

**Masking is the first use.** An evaluation runs the agent over a variant or case whose answer is already known, and the
run is worth nothing if the agent can see the knowledge that settled it. So an evaluation session carries a **data
cutoff**, and every evidence rpc the agent calls takes it: the databases answer as of that date, and the tools run at
the versions consistent with it. The time-varying sources, and what each answers as of:

- ClinVar records, by the date they were evaluated;
- the literature indexes and the corpus, by publication date;
- population frequencies, by release;
- functional-assay deposits, by deposit date;
- gene–disease curations, by their assertion dates;
- specifications, by version.

The tools are pinned with the date because a predictor is not timeless, even though its answer is a deterministic
function of its input: a splice model trained on later variants carries the knowledge the cutoff exists to withhold, and
an annotation tool's later release changes what it says about the same variant. So normalisation, effect and splice
prediction and transcript structure take context exactly as the databases do; the one exposed rpc that takes none is the
`hello` smoke test's.

A gate that already stands on an rpc that will take the cutoff stays until the cutoff gives it its use, rather than
being removed to be re-added; no gate is added ahead of its use.

Masked means absent, never refused. A record after the cutoff, or a paper published after it, does not exist for the
session: it is answered as unknown, indistinguishable from a record that was never there. A refusal that said "exists,
but not for you" would hand the agent the existence signal the cutoff is there to withhold — the same rule the workspace
model applies across Projects ([`workspace-model.md`](workspace-model.md)).

The cutoff is an attribute of the session record and comes back with the Project and Analysis when a token is resolved.
It enters once, when the Analysis is created, through the create path the evaluation harness uses, and nothing later can
move it. It is never an argument the caller passes on a call, because the caller under evaluation is the one with the
incentive to move it, and never request metadata the worker injects, because a value resolved from the session record is
pinned to the session and auditable where a header is whatever the sender said. An rpc that applies a cutoff and
receives no session cannot know whether there is a cutoff to apply, so it refuses; the concrete failure that rule
prevents is a run that quietly saw the future. Which date a harness pins — the day a curator recorded the reference
classification, say, so the agent sees what the curator could have seen — is the harness's decision, made in its own
design; the run-review loop is the first such harness.

### Callers

The agent's every call carries the session: the worker injects the token into each forwarded call, and the guest has no
other way out. Whether the rpc uses it is the rpc's decision under the rule above, and the agent's code is none the
wiser either way.

The BFF presents a session only to an rpc that takes context, deriving the token of the Analysis the work is done in. To
an rpc that takes none it presents nothing beyond its own service identity, which IAM has already verified. The
browser's paper reads — a paper's renderings and files, the location of its content, the position of a quote — take no
context: the corpus is shared, a curator opening a paper is not an evaluation subject, and nothing paid starts. So the
browser contract for those reads names no Analysis, and the BFF derives no token for them.

An rpc both callers reach answers both identically, which under masking means an rpc that takes context is shared only
with a caller that presents a session; a caller that presents none is refused, not answered unmasked. That is what lets
the service stay one service: the difference between the callers is which rpcs each one reaches and what each one
carries, never what an rpc does. No literature rpc is shared: the eight the agent calls all take the cutoff, and the
three the browser calls take nothing.

### The literature service, as the worked instance

The agent's reach into the literature is the discovery searches, resolving identifiers to papers, starting and polling
full-text production, reading a paper's markdown and validating a quote; each of those rpcs is marked, and the change
that marks them argues the cost and destination clauses for each. A paper's renderings and files, the location of its
content and the position of a quote serve the BFF's paper display alone and are not marked, so the agent's catalog never
lists a storage location, and the types only those replies use — the content location, the quote geometry — are cut from
the guest's copy of the contract. Starting full-text production is paid work and attributes the conversion to the
Analysis that asked; the searches answer by publication date and a paper published after the cutoff is absent for the
session on every agent rpc, ingestion and readiness included, so a PMID the model remembers from training buys it
nothing; the paper reads the browser makes carry no session at all.

## Alternatives considered

- **A file option.** Rejected because callers cross-cut a service and the literature file is the instance: three of
  eleven rpcs are the browser's, and the file-level remedy was to split the file by caller. The property the file-level
  flag bought — the shipped stub is the exact surface — is supplied by the guest contract tree, which is exact at every
  layer the guest has.
- **A service option.** The same problem one level up: literature is one service.
- **A generated facade over protoc's whole-service stub**, offering only the marked methods. Rejected: a second object
  to explain, carrying none of the rpc comments, with everything unexposed still shipped beneath it — the types, the
  docstrings and the option that selected them. Cutting the contract before protoc runs gives the model one artifact
  that is exactly its surface.
- **Pruning the descriptor and feeding it to protoc** instead of cutting the source. Rejected: comments are addressed by
  path index in a descriptor, so a naive prune moves a dropped rpc's comment onto its neighbour and needs a remap to
  stay honest, and a descriptor cannot yield the `.proto` text the model reads. The same source info that would drive
  the remap supplies the spans to cut the text directly.
- **A session gate at every exposed rpc**, as a clause of the exposure condition. Rejected: the guest cannot forge a
  token, IAM bounds the callers, and a check that discards what it resolved guards nothing. Stated as a property of the
  rpc, it reached trusted callers the condition was never about, which forced the browser contract to name an Analysis
  for reads of a shared corpus.
- **No session at the data plane at all**, relying on IAM and the hatch. Rejected: masking and attribution need the
  session resolved at the rpc that applies it, and "where the answer depends on it" admits exactly those without a
  blanket.
- **A cutoff for the databases but not the tools.** Rejected: a predictor trained on later data, or an annotator's later
  release, carries the knowledge the cutoff withholds, and the agent cannot tell a leak through a tool from one through
  a lookup. The tools are pinned with the date.
- **The cutoff as request metadata the worker injects**, rather than a session attribute. Rejected: metadata is what the
  sender said, an attribute of the session record is pinned and auditable, and the BFF's leg would have to inject too.
- **Comments or tags on the rpc instead of an option.** Rejected: comments are stripped from the descriptor, invisible
  to `buf lint` and `buf breaking`, need source scraping, and cannot express a fail-closed default — a missing comment
  is indistinguishable from a forgotten one.
- **A generic byte-forwarding proxy** (one handler, no per-service forwarder). Rejected: the hatch exposes only
  concrete-servicer registration, so this needs an API addition upstream; and a generic handler still needs the
  descriptor for each method's cardinality plus hand-rolled method-to-channel routing. Hand-written per-service
  forwarders over the generated servicer base use the existing API and inherit its typing and cardinality.
- **A hand-authored allowlist verified against the proto in CI.** Rejected: heavyweight and not correct by construction,
  where generation is. The deliberate-review property it aimed for is supplied by the co-signature and the committed
  allowlist diff.
- **A runtime registry as the source of truth**, deriving the allowlist from registered forwarders. Rejected as the
  root: the guest cannot import the worker's registry, and the rootfs is built before any worker exists, so the shipped
  stubs need a source readable statically at build time, which the proto option is.
- **Marking every guest-visible file**, message-only files included. Rejected in favour of inferring the shipped
  messages from the marked rpcs' signatures: that set is already fixed by the contract, so explicit marking is redundant
  and can omit a needed file.
- **The shipping rule as a `buf lint` rule.** Rejected: the rule set cannot express a cross-file closure, and a custom
  lint plugin costs a pinned binary. `regen` already reads the descriptor set to emit the artifacts, so the rule lives
  there and the freshness check gates it.

## Open questions

- The entitlement check for a licensed paper on the browser leg: keyed on the user, outside this doc, and undesigned in
  the literature layer.
- The decorator's name and module for the co-signature on the servicer method, and where the cross-check imports the
  servicers from.
