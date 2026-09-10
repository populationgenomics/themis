# Design: data-plane rpc authorization

**Related:** [`security.md`](security.md) (the default-on chokepoint rule this is an instance of),
[`sandbox-worker.md`](sandbox-worker.md) (the hatch every call from the agent crosses),
[`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) (the `agent_exposed` proto option this reads admission from),
[`evidence-fulltext.md`](evidence-fulltext.md) (the paper-ingestion spend this re-gates),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the interface this converts, and the corpus it serves)

## Overview

The **data plane** is the set of backend gRPC services under `themis/services/` that hold the evidence an Analysis draws
on. Most of its rpcs authorize the same way: read the caller's session token, resolve it through the **auth service**
(the service that issues and resolves session tokens), and proceed if it resolved. The binding that resolution produced
is then thrown away, so the check answers exactly one question — does this caller hold a session? — and on the
literature interface it is not asked at all, because those reads carry no gate.

Neither shape fits the callers literature has. The **web tier** — the server behind the browser, which renders the
workbench and calls backend services on the browser's behalf — reads the **corpus**, the store of published papers
shared across every Project, presenting its own verified identity and no session token. The agent-facing reads, the ones
that should be gated, are open. And a third fact is coming that a boolean cannot describe at all: fetching a paper an
institution subscribes to draws on a *person's* entitlement, so the call has to know whom it is made for.

Authorization therefore stops asking one question and assembles the facts a call arrives with. There are three, and
every internal call carries at least the first:

- the **caller** — the **first-party** service account (one of ours: the web tier, a worker, a maintainer's automation
  account) verified from the ID token Cloud Run forwards. Always present.
- the **binding** — the Project and Analysis the call is scoped to, resolved from a session token. Present when the call
  is made within an Analysis.
- the **person** — whom the call is made for, resolved from an approval token that person granted in the browser.
  Present only when the call exercises something only they may.

An interceptor assembles those three into one `AuthContext` for every call and checks it against what the **contract**
admits. The proto already marks an rpc the agent may reach (`agent_exposed`); it gains two sibling options, one
admitting any call scoped to an Analysis and one naming the first-party **callers** — drawn from a closed enum of our
service accounts, `CALLER_WEB` say — that may call without a session. The interceptor derives the admission rule from
those options, so who may call an rpc is stated once, in the contract, and read from there by the hatch allowlist and
the interceptor alike. A call the contract does not admit is denied, as is a call to an rpc whose contract admits
nobody; denial is the default rather than a fallback.

The line that keeps this from growing into an access-control system of its own: **what a call sees is never selected by
the person driving it.** It may be selected by the Analysis the call is scoped to; a person's entitlement can widen what
is *acquired* into the corpus, and nothing else. That leaves a question this doc raises and does not answer — a paper
acquired on one institution's subscription lands in a corpus every Project reads — so the person axis is designed here
and held off by a check that fails loudly if anything presents one.

The one identity the agent can present — the sandbox-job service account, one of ours like the rest — has no member in
that enum, so no contract can admit it: an agent that dropped its session token satisfies no caller arm, and the
exclusion is a property of the type rather than of any configuration.

## Background

A **Project** is the access boundary: the datasets and users associated with a body of work. An **Analysis** is a body
of collaborative work bound to one Project, and it is the unit this design scopes on — coarse deliberately, since the
workspace behind it is shared among everyone in the Project, and anything finer leaks through that sharing.

A **session** is what the auth service issues so a call can name an Analysis: a short-lived token that resolves to a
Project and Analysis id ([`security.md`](security.md), and the session client at
[`themis/clients/auth/session.py`](../../themis/clients/auth/session.py)). A session is the *presentation* of a scope,
not a property of the caller presenting it — nothing about a resolved binding says the caller was the agent, and the
design does not read one from the other. Tokens are minted by **deriving** them: a first-party account granted the
signing permission on the session-token key can compute the bearer for any session id, and so present itself as any live
session. The web tier derives one at session create and the dispatcher derives one per sandbox launch; a maintainer's
automation account may be configured to as well. An account that can derive a session can therefore reach any Analysis's
scope — a property of session minting that no rule in this design changes.

The **sandbox agent** — untrusted model-driven code running in a network-isolated guest — reaches an internal service
only through the worker's **hatch**, which dials the service presenting the sandbox-job service account's identity and
injects the agent's session token as the call's metadata ([`sandbox-worker.md`](sandbox-worker.md)). Two facts about the
hatch are load-bearing here: the guest holds no credential of its own and cannot mint one, and the guest's own metadata
does not cross the hatch. So to any internal service the agent appears as exactly one identity, carrying whatever the
worker injected and nothing it chose itself. One worker serves one session and holds one token, which bounds what a
compromised worker can reach in the *workspace*; the corpus is shared, and one token reads it as any other does.

The construction buys one invariant: **the agent can neither see nor choose whom a call speaks for.** It is not that
nothing on the agent's path may speak for a person — the acquisition path below does exactly that — but that the agent
never holds the credential, never learns the identity, and cannot manufacture one, because everything it presents was
injected on the other side of the hatch.

Internal calls authenticate with Cloud Run's own IAM: the caller presents a Google-signed ID token in the standard
`Authorization` header, and Cloud Run rejects the call unless the caller holds `run.invoker`. After validating that
permission, **Cloud Run forwards the `Authorization` header to the container unchanged**, signature intact, so the
container can re-verify the token and read the calling service account's email from it. (This is the documented
service-to-service behaviour, and not the same as IAP, which consumes the caller's credential and injects its own
identity header. The confirmation and its source are in the Appendix.) Without it there would be no verified caller to
authorize on at all.

An internal call can therefore carry credentials that do different jobs. The ID token is **reachability**: it clears
Cloud Run's invoker gate, but any identity granted `run.invoker` can mint one, so on its own it says nothing about which
data the call is for. The session token is **scope**: the callee resolves it to one Analysis, and what the call may
touch follows from that. An approval token, described below, is **entitlement**: it says a named person agreed to this
call drawing on what is theirs.

The web tier is already in the first position: the browser's paper-content pane reaches the literature service directly,
over a gRPC client presenting the web tier's own ID token and no session token (the live adapter at
[`apps/web/src/server/adapters/live/literature.ts`](../../apps/web/src/server/adapters/live/literature.ts)). That is why
those reads went ungated rather than gated the way the other interfaces are — a session gate had nothing to check.

## Non-goals

- **Masking.** No ablation of the corpus — a restricted view, a licence-bound representation, a data cutoff — is
  designed here. The one step a masking layer would fill is named under "What a call may see"; nothing sits behind it.
  Masking is its own design, and the licensing question under Open questions is what will force it.
- **The approval flow.** How a person is asked to approve a fetch, what the request shows them, how the answer reaches
  the service, and the message the person resolves to are a design of their own. This doc names what the resulting token
  must be — resolvable server-side, unforgeable by the service that relays it, and bound to what was approved — and
  takes that flow as one of the two prerequisites before anything is configured to resolve one.
- **Deploy wiring.** The GCP project that completes a caller's service-account email, and a service's own URL for token
  verification, are deployment configuration. This doc names the seam; the values are set where the image is deployed.

## Design

The primitive is a context assembled per call, an admission rule derived from the rpc's contract, and the interceptor
that assembles the one and enforces the other. It lives alongside the session client it resolves through, in
[`themis/clients/auth/`](../../themis/clients/auth/).

### One context, built for every call

A server interceptor builds one `AuthContext` for every call from the three facts the Overview names, then derives the
admission rule of the method being called and evaluates it. The binding is a proto because it is what a resolution rpc
returns; the person will be, for the same reason, once the approval flow defines it. The context around them is a frozen
dataclass, so an absent binding can be absent — the reason is under Alternatives.

```python
@dataclasses.dataclass(frozen=True)
class AuthContext:
    caller: str
    session: auth_pb2.SessionContext | None
    on_behalf_of: UserContext | None       # the approval flow's message; nothing resolves one yet
```

### What the context guarantees at construction

Some combinations of the three facts are bugs rather than policy questions, and the context refuses to exist rather than
being handed to a rule that would quietly deny. Each surfaces as an internal error, not as `PERMISSION_DENIED`, and each
is relaxable later by an explicit edit rather than by omission:

- **The agent's account without a binding.** The hatch always injects a session token, so its absence is a worker defect
  or a compromise. This is the one check that reads the caller's identity for a reason other than admission: it compares
  two facts the call arrived with for consistency, and decides nothing about who may call.
- **An approval token with nothing configured to resolve it.** The person axis is held off until its two prerequisites
  land, and a deployment convention is not a property: a call carrying an approval token while no resolver is configured
  is an error, not an ignored header. Switching the axis on is the edit that configures the resolver.

The exclusion the caller arm rests on — that the sandbox-job account is never admitted by identity — is not a check at
all but a fact about the contract: the `Caller` enum has no member for it, so no rpc can name it. Two layers back that
up. The sandbox-job account is a required deployment input, so a service given none refuses to start, and the caller arm
refuses that identity per call whatever the contract says; and a test binds the enum's account ids to the accounts the
infrastructure declares, so a member cannot name an account that does not exist or go stale when one is renamed.

The interceptor is the only reader of authorization metadata. A **servicer** — the class implementing one gRPC service —
never reads the call's metadata itself, which a test asserts over the servicer modules, so a header naming a person, or
anything else, has exactly one place it could be honoured and that place does not honour it.

### The contract declares admission; the interceptor derives the rule

Which rpcs the sandbox agent may reach is already declared in the proto, by the `agent_exposed` method option the
hatch's allowlist and the guest's stubs are generated from ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)). This
design adds two sibling options, defined with it in
[`schema/proto/themis/rpc/sandbox_options.proto`](../../schema/proto/themis/rpc/sandbox_options.proto): `admits_session`
admits any call scoped to an Analysis, and a repeated `admits_caller` admits each named first-party caller, no session
required. `agent_exposed` stays what it is — the codegen marker for the guest's surface — and *implies*
`admits_session`, since the agent only ever presents a session, so an exposed rpc cannot fail to admit one. The rule the
interceptor derives is the alternation: a call scoped to an Analysis is admitted where the rpc is exposed or admits
sessions, a call from a named caller where the rpc names it, and an rpc setting none of the three admits nobody. A
reader of the proto learns who may call an rpc, and the servicer carries no admission code at all.

Keeping `admits_session` distinct from `agent_exposed` is what lets a session-scoped call arrive from something other
than the agent — the web tier acting within an Analysis, say — without widening the guest's generated surface to reach
it. The two options answer different questions, and only one of them changes what the sandbox can call.

A caller is a member of a closed `Caller` enum, one per first-party service account, each member carrying the account's
id as an option on the value:

```proto
enum Caller {
  CALLER_UNSPECIFIED = 0;
  CALLER_WEB = 1 [(themis.rpc.account_id) = "themis-web"];
  CALLER_CLU = 2 [(themis.rpc.account_id) = "themis-clu"];
}
```

The contract cannot carry a service account's email, which differs per environment, but the account *id* is the same
everywhere — the infrastructure names every first-party account literally, and only the GCP project varies — so the
verifier completes it with the project the service runs in. An enum rather than a string is what makes a misspelled
caller a compile error instead of an rpc that silently admits nobody, and it is what makes the sandbox-job account
unadmittable rather than merely refused: it has no member. Adding a first-party caller is adding a member, which is
additive and, like every change to an option's value, not a breaking change under `buf`. Naming callers in the contract
is a deliberate widening of what `agent_exposed` began: the proto already stated one class of admitted caller, and it is
the one place a reader looks for the rest.

Admission is an allowlist and only an allowlist: the contract names what an rpc admits, a call matching none of it is
denied, and there is no way to name a caller that is *refused*. A denylist beside the allowlist would not change the
default, but it would be a second statement of who may call, able to disagree with the first — and the only occasion for
one is an admission drawn too wide, which is fixed by narrowing it.

Fail-closed governs a single call the same way, in two stages. Verifying the caller comes first, before any context
exists, and a call whose ID token the service cannot verify is denied outright, on no arm: Cloud Run admits no call
without a valid token, so one the service cannot re-verify is a deployment fault — the audience set wrong — or a broken
guarantee, and neither yields a caller. That is why `caller` is never absent from a context. Within a built context, an
arm that cannot decide declines: a session token that does not resolve is no binding, and a verified caller the rpc does
not name is no admission. Admission is reached only by an arm affirmatively matching. A failure that is not a decline —
the auth service being unreachable, say — propagates as the failure it is, because reading an outage as "no session"
would turn an incident into a silent policy decision.

### Default-deny is enforced at the interceptor, over the whole server

An rpc whose contract admits nobody is denied by the interceptor rather than reaching its body. A check written inside
each method could not promise that: a method nobody remembered to gate runs no gate at all and is reachable by anything
that cleared Cloud Run's invoker gate, and only a test could notice. The interceptor makes denial a property of the
running server, the posture the web tier takes with the check it runs on every browser request before any handler
([`security.md`](security.md)).

A gRPC interceptor is server-wide by construction: it sees every inbound call by its full method path before any handler
runs, and it has no notion of which service the path belongs to. So its reach is not a list of services it governs — a
list would be a second registry, and an interface added to the server but not to the list would run ungated, the
omission this design exists to make impossible. It derives a rule for every path on the server, denies a path it cannot
resolve to a contract, and exempts exactly one name: the health check, which Cloud Run probes without credentials, as
the web tier exempts its own. Every evidence interface hosted with literature already marks its rpcs `agent_exposed`, so
the derivation covers the whole server from the first deploy; what the in-body session checks those interfaces carry
become is redundancy, and deleting them is an open question below rather than a precondition.

### The binding names an Analysis, not a session

What a session token resolves to is a Project and an Analysis. The design reads the Analysis from it and nothing else:
scope is a fact about the work, and the session is one way for a call to present it. So a rule that wants to know
whether a call is scoped to an Analysis asks that, rather than whether the caller looks like the agent — the conflation
this design exists to remove.

The corollary: the session arm of an admission is a scope gate, not a caller gate. Any holder of a session bearer
satisfies it — the agent, and every account that can derive one. Admitting a session says the call is scoped to an
Analysis; it says nothing about who scoped it, and an rpc that needs to know who must name the caller.

### The person is resolved, never asserted

The obvious way to carry a person is for a trusted caller to assert one in a header. That fails on the one path that
most needs it. The agent's calls are relayed by the sandbox worker, the process sitting closest to model-driven code,
and a plaintext claim is something that process can fabricate. A token it merely relays is not: the web tier mints it
when the person answers, so the worker can pass it on and cannot invent one. So a person reaches the data plane in the
same shape as a scope does:

| fact        | presented on the call                                        | resolves to                        |
| ----------- | ------------------------------------------------------------ | ---------------------------------- |
| scope       | session token, minted when the Analysis's work is dispatched | the Project and Analysis           |
| entitlement | approval token, minted when the person approves the request  | the person, and what they approved |

The person's attributes never transit the sandbox host, and expiry and revocation come from the same table shape the
session token already uses.

Because the approval is what the token resolves to, binding a person to what they agreed to is not a second mechanism
beside the identity: it is the identity's provenance. That binding has to be enforced, and a rule cannot do it — a rule
sees the context, not the request. The agent chooses which paper to ask for even though it cannot choose whose
entitlement to draw on, so the acquisition rpc's body compares the papers requested against what the approval names and
refuses the rest. Two consequences for the approval flow follow. The hatch fixes a call's metadata once per worker
execution, so an approval cannot ride as metadata the way a session does: it travels in the acquisition request itself,
which changes that request's shape. And a token good for one approval is good for exactly the papers it names, so a
second acquisition needs a second approval.

### What a call may see

What a call may see is never selected by the person driving it. On the agent's path the literature backend **port** —
the interface the servicer reaches its storage through — receives the context and resolves the corpus view from its
Analysis; today that is the identity, every Analysis resolving to the whole corpus, and it is the one step masking would
fill. The alternative, a view that varies with whoever is driving, breaks two things: two curators opening the same
Analysis would see different evidence, so the agent's conclusions could no longer be audited against what a reviewer
sees; and the workspace behind an Analysis is shared, so anything one person's view pulled in is visible to everyone
else in the Project anyway.

The browser's paper-content reads are the exception the seam does not cover: they admit the web tier and carry no
Analysis, so there is nothing to resolve a view from. When masking needs a scope on that path, the web tier can present
the Analysis the pane is open in, since it derives those sessions; until then the browser reads the whole corpus as the
agent does.

What a call may **acquire** is the different question, and the only one a person answers. Pulling a paper that is not
openly available into the corpus draws on a subscription belonging to a person and an institution, and no Analysis-level
fact can stand in for that. So the person is carried on the acquisition path — `MaybeIngestPapers` — and nowhere else.
But the corpus is shared across every Project, and [`PRODUCT.md`](../PRODUCT.md) §7 holds that licensed literature is
never shared across institutional lines. That exposure is not hypothetical: the literature contract already marks a
rendering `SUPPLIED` when its text reached the corpus through a human's institutional access, and such a rendering is
served to any Analysis and to the browser today. This design leaves that as it finds it and declines to widen it: a
person's entitlement drawn on at scale would turn an occasional supplied rendering into the corpus's normal way of
growing. The person axis is therefore designed here and held off — the construction check above refuses an approval
token until a resolver is configured, and configuring one waits on the approval flow and on masking able to hold a
licensed representation to the licences that reach it.

### Adoption: the literature interface

Literature adopts the primitive first because it is the interface with a caller that holds no session and the interface
with a **spend** to attribute: `MaybeIngestPapers` may start a **conversion**, the rendering of a paper's PDF to
markdown by a model, which costs model budget. The other evidence interfaces are covered by the interceptor from the
same deploy, as described above, and keep their in-body session checks until those are deleted.

Literature's rpcs split three ways:

- The **agent-facing reads** — searching the indexes, reading a paper's markdown, validating a quote — are
  `agent_exposed`, so they admit a call scoped to an Analysis. These are the reads that carried no gate, so this is
  where the new boundary falls.
- The **paper-content reads** the browser renders admit `CALLER_WEB` and, for a maintainer acting on the corpus by hand,
  `CALLER_CLU`. These are the rpcs the proto leaves unexposed.
- The **producer**, `MaybeIngestPapers`, is `agent_exposed` and admits `CALLER_CLU`. It is the one rpc whose
  implementation does more with the context than hand it to the port: its spend is charged to what the call was made
  for, and it is the acquisition path a person's entitlement will travel on.

```proto
rpc MaybeIngestPapers(MaybeIngestPapersRequest) returns (MaybeIngestPapersResponse) {
  option (themis.rpc.agent_exposed) = true;
  option (themis.rpc.admits_caller) = CALLER_CLU;
}
```

A read carries no admission code at all — its admission is in the proto and its gate is the interceptor. What every
corpus read does do is hand the context to the port, which resolves the view from it; that is the masking seam, present
on every read from the start, and not an admission decision. It selects the whole corpus today.

`CALLER_CLU` is `themis-clu`, the account a maintainer impersonates to call a backend by hand: it holds `run.invoker` on
the evidence service already, and unnamed it would be refused every literature rpc the moment they gain admissions.
Where a stack also lets it derive session bearers, it can satisfy the session arm as any live session, as the web tier
and the dispatcher can everywhere — the property of session minting stated under Background, not one its admission adds.
Whether it may reach a given environment's service at all stays IAM's question, decided by `run.invoker`, not the
contract's.

The producer's admission replaces a narrower gate that resolved a session only on the enqueue step and discarded the
binding, so a batch with nothing to enqueue was answered without any token and no spend was attributable
([`evidence-fulltext.md`](evidence-fulltext.md)). Now the whole rpc authorizes up front, a caller admitted by nothing is
refused before any work, and the binding reaches the enqueue. That makes attribution *possible*; where the record is
written is an open question below.

## Alternatives considered

- **Keep the boolean session gate and special-case the web tier.** Rejected: it re-answers "who may call this" per rpc
  with ad-hoc exceptions, the opt-in shape [`security.md`](security.md) rejects, and it still discards the binding that
  attribution needs.
- **A union of mutually exclusive principals** — a call resolving to *either* a session *or* a verified service
  identity. Rejected on two counts. It discards: a verified caller is present on every internal call by construction, so
  returning one fact and dropping the other loses attribution for no gain. And the exclusivity is false — an approved
  acquisition is scoped to an Analysis *and* made for a person, which a union cannot express.
- **`AuthContext` as a proto message.** Rejected: a generated message field cannot be absent, so an unscoped call would
  read as scoped to an empty Analysis instead of failing to type-check; messages are mutable, and a handler must not be
  able to edit the authorization it was handed; and the construction checks would have nowhere to live. The context
  crosses no boundary, so it gains nothing from being a contract.
- **Roles — named sets of accounts filled by deployment configuration — instead of callers.** Rejected: the indirection
  bought environment-independence that the account id already has, at the price of a role-to-accounts map in every
  deployment, a startup check for a configured role the contract never names, a silent outcome for a named role the
  deployment never fills, and a sandbox-job exclusion that lived in configuration rather than in the type. With a dozen
  first-party accounts and two or three per rpc, a reader is better served by seeing exactly who. What a role could do
  that a caller cannot — admit in one environment and not another — is IAM's job, done with `run.invoker`.
- **A per-method decorator declaring admission** (`@admits(Session(), Operator(roles=...))`). Rejected: it is a second
  statement of a fact the contract already half-states, so it needs a test to reconcile the two, and default-deny then
  depends on every method being decorated — a decorator left off runs no gate. What it bought and this loses is a
  statically narrowed session handed to the body; the body narrows once instead.
- **No declaration; each method checks what it needs.** Rejected: it holds only for methods that consume the context —
  an acquisition without an affiliation fails on its own. A read has no natural reason to touch the context, so a
  forgotten check is silent, and every such method is open to any holder of `run.invoker`. That is the state this design
  replaces.
- **`agent_exposed` as the session admission, with no `admits_session`.** Rejected: the option drives codegen for the
  guest's surface, so admitting a session-scoped call from anything other than the agent would mean generating guest
  stubs for an rpc the agent has no business calling. One bit cannot carry two independent decisions.
- **A user-scoped credential inside the sandbox** — the agent holding something that speaks for the curator whose
  Analysis it is running. Rejected: the agent executes model-written commands over web-derived context, so a credential
  there lets prompt-injected code act as that person across every Project they belong to. What the agent relays instead
  is a token it cannot mint, scoped to one approved request.
- **The sandbox worker asserting the Analysis alongside the session.** Rejected for now, and not because the worker is
  untrusted: it already chooses which session token to inject, so it is already an authority on scope. It is that the
  worker deliberately never learns the Analysis id — the Sheaf service, which serves the workspace, scopes its access by
  the session token ([`sandbox-worker.md`](sandbox-worker.md)) — so asserting it would add a fact to that process in
  order to save a resolution that is not a cost, and would leave two statements of one scope with no way to adjudicate a
  mismatch.

## Open questions

- **Whose licence a shared corpus follows.** [`PRODUCT.md`](../PRODUCT.md) §7 is written per user: opening a paper
  "follows the user's institutional licensing". The candidate that reconciles that with an Analysis-scoped view is per
  *representation*, not per paper — facts and **knowledge units** (the structured claims extracted from a paper) pooled,
  full text held to a licence — with the open choice being whose licence: the reading user's own, which is §7 as
  written, or the union of the Analysis's Project members', on the argument that members could and routinely would share
  a paper among themselves. The union hands one institution's text to another's member in a Project that spans two, so
  it needs an explicit case made against §7, not an assumption. Either way the corpus has to record which person and
  affiliation obtained each licensed representation, and the view has to be a predicate the index query incorporates
  rather than a filter over results, since filtering afterwards breaks pagination and leaks in the counts what it
  removed.
- **Where a conversion's spend is recorded.** The binding reaches the enqueue, but the conversion runs later in a worker
  off a task named by the paper, so attributing the spend means the binding rides the task and lands somewhere the cost
  pipeline can read. Which store, and keyed by session or by Analysis, is unsettled.
- **Deleting the in-body session checks on the other evidence interfaces.** Their rpcs are all `agent_exposed`, so the
  interceptor gates them from the first deploy and the check each method opens with is redundant. Removing it is
  mechanical and changes no behaviour.
- **A deployed probe for the Cloud Run header behaviour.** The forwarding fact is documented and code-backed (see
  Appendix), and the offline fixtures do not depend on it, but the path is not exercised against a real Cloud Run until
  a probe on dev confirms the forwarded token verifies. Worth doing before it is relied on in production.

## Appendix: the Cloud Run header fact

The verified caller depends on Cloud Run forwarding the caller's `Authorization: Bearer <ID token>` to the container
after it validates `run.invoker`. Google's service-to-service authentication documentation confirms it two ways: the
receiving-service section ships sample code that reads the token straight off the `Authorization` header and re-verifies
it, which is only possible if the full signed token reaches the container; and the note on the alternate
`X-Serverless-Authorization` header states that *that* header — offered for apps that want to repurpose `Authorization`
for their own scheme — is the one whose signature is stripped, which is informative only because the default header's is
not. The canonical server-side read of the caller identity is therefore: re-verify the forwarded token against Google's
public certs with the audience set to the receiving service's own URL, then read the `email` claim, failing closed when
the token is absent, unverifiable, or carries no verified email. Source:
`https://cloud.google.com/run/docs/authenticating/service-to-service`.
