# Design: the sandbox worker

**Status:** current **Related:** [`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) (which rpcs the guest may call
through the hatch, and how that set is declared), [`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md)
(the dispatcher, the credential model and the session lifecycle this execution model sits inside),
[`../runbooks/self-hosted-sandbox.md`](../runbooks/self-hosted-sandbox.md) (operating it), [`services.md`](services.md)
(the data-plane services it reaches)

## Overview

Themis runs the agent's tool execution inside CPG's network rather than Anthropic's, because that execution reads
genomic data and calls internal services ([`../PRODUCT.md`](../PRODUCT.md) §9). This doc decides what runs where inside
that boundary: how a session's code is isolated, what it can reach, and how its work survives the process ending.

- **One trusted process, not two containers.** The worker holds the credentials and runs the SDK's session loop.
  Untrusted model code never shares it: every shell command is marshalled into a
  [bubblewrap](https://github.com/containers/bubblewrap) guest whose only exit is a method-allowlisted gRPC hatch.
- **Egress is contained at the namespace, and fails closed.** The guest runs with an empty network namespace, so there
  is no egress firewall, no DNS sinkhole, no internal load balancer and no proxy in front of Anthropic — a whole network
  subsystem the container-isolation shape needs and this one does not.
- **The guest holds no credential and names no upstream.** It dials the hatch; the trusted worker injects the session
  token and forwards.
- **The workspace is checkpointed when a sandboxed command returns, and once more on the way out** — not when a turn
  ends, because the turn boundary is not observable from the worker. Between checkpoints the session's files exist only
  in the container, so what an ungraceful exit loses is bounded by the last command rather than by the turn.

## Background

**Where the worker sits.** Anthropic runs the model and owns the conversation; the tools run here. A webhook tells our
dispatcher that a session wants work, and the dispatcher starts one Cloud Run Job execution for it, injecting the
session's identifiers and its minted session token. Everything upstream of that — the dispatcher, the credential seam,
the work-queue lease — is the plan's ([`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md)) and
unchanged by this design; what follows begins at the moment that execution starts.

**Vocabulary.** A **session** is one agent conversation; a **work item** is the claim on it that the SDK's worker loop
leases. The **guest** is the sandboxed process model-written code runs in; the **hatch** is the Unix-socket gRPC server
that is its only exit. **`/workspace`** is the directory the session's files live in, and the only writable one the
guest sees. In **code mode** the model reaches internal services by writing Python against their generated stubs rather
than through chains of discrete tool calls.

**What the SDK's loop does, and does not, hand us.** Four of its behaviours shape decisions below:

- It consumes the session's event stream itself and yields the caller *tool calls*, not events. The turn boundary is
  visible to it and not to us.
- It releases the work item when the session terminates, or after an idle period elapses following the end-of-turn
  event.
- It runs each tool call under a per-tool deadline and abandons a call that overruns, so nothing a slow call was still
  doing completes.
- It downloads the session agent's skills into the working directory each spawn, which fixes that directory as
  `/workspace` so the guest finds them where the platform's convention says they are.

**What postern gives us.** [postern](https://github.com/populationgenomics/postern) is the sandbox library: a bubblewrap
guest with an empty network namespace, no capabilities, a non-root uid, its own user namespace and a seccomp filter that
fails closed on an unrecognised architecture; a read-only root filesystem the image assembles ahead of time; a
reference-closed workspace accessor that resolves one path component at a time, so a symlink or special file the guest
planted is never followed out of the tree by the trusted process; and the hatch, which refuses any method outside an
allowlist. It also offers a self-test that launches a real guest, which is proof of all of it because every one of those
controls aborts the launch rather than degrading quietly — and the host matters, because unprivileged user namespaces
are what bubblewrap needs and not every runtime provides them.

The design was pressure-tested in two adversarial security reviews against postern, both concluding that it can run
without an egress firewall or a load balancer.

## Non-goals

- **No general-purpose method on the hatch.** Nothing shaped like `fetch(url)` or `query(sql)`. Every exposed rpc
  derives its scope server-side from the verified session token, never from an argument the guest chose; a general
  method would move that decision inside the sandbox.
- **The store is not agent-reachable.** The service holding the session's documents is worker-only, so no hatch method
  reaches it. A guest-facing store API, were one ever wanted, would have to be scoped and size-capped by design rather
  than being the raw document rpcs put on the allowlist.
- **The guest's egress is not filtered, because it has none.** This design expresses no allowlist of hosts or CIDRs and
  has nowhere to put one. Containment is the absence of a network, not a policy over one.
- **File I/O is not sandboxed** — only arbitrary execution is (§"Only arbitrary execution is sandboxed").

## Design

The worker is one Python package, [`themis/services/sandbox_worker/`](../../themis/services/sandbox_worker), running as
one Cloud Run Job execution per session:

```mermaid
flowchart TB
    wq["Anthropic work queue"]
    wq -->|"dispatcher: HMAC webhook → jobs.run<br/>(one execution per session)"| worker

    subgraph job["Cloud Run Job — one container, one trusted process"]
        worker["the SDK's session loop<br/>holds the environment key"]
        hatch["the hatch<br/>UDS · method allowlist · session-token injection"]
        subgraph guest["bubblewrap guest — postern"]
            iso["empty netns (no egress) · no capabilities · seccomp<br/>non-root · RO rootfs · RW /workspace"]
        end
        worker -->|"shell(command, intent) marshals in"| guest
        guest -->|"only exit out"| hatch
    end

    worker -->|"calls directly (no proxy)"| anthropic["Anthropic API"]
    worker <-->|"checkpoint/restore /workspace<br/>reference-closed accessor, direct"| store["themis-store<br/>(internal ingress)"]
    hatch -->|"forwards the allowlisted rpcs<br/>(session-token injected)"| upstreams["the agent-exposed services"]
```

### One trusted process, not two containers

The obvious shape is two containers: an **untrusted** one running the SDK's worker with bash and the file tools
executing inside it, beside a **trusted** sidecar holding the credentials and injecting them. That shape puts model code
in a container that must then be contained *from the outside*, and containing a container's egress takes a whole network
subsystem — a dedicated VPC, a deny-all egress firewall with an allowance for Anthropic, a NAT, a DNS sinkhole, an
internal load balancer for the services the sandbox may still reach.

Pushing untrusted execution into an empty network namespace instead needs none of it. The worker is one trusted process;
it holds the environment key and the session token, calls Anthropic itself, and marshals each command into a guest that
has no network to contain. Egress containment becomes fail-*closed* — a namespace with no interface — rather than
fail-*open*, which is what a firewall is when a rule is wrong.

The table below is the consequence, kept because each absence is a decision a reader will otherwise propose filling:

| Not built                                                                                         | Why it is unneeded                                                                                                                                                                                        |
| ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| An internal load balancer                                                                         | Its only job is a host-filterable private address for internal services, which only a host-filtering firewall needs. No firewall, no filtering requirement: the worker dials each service at its own URL. |
| A dedicated VPC, a deny-all egress firewall with an Anthropic allowance, a NAT and a DNS sinkhole | The guest's empty namespace contains egress at the namespace, fail-closed.                                                                                                                                |
| A reverse proxy in front of Anthropic, with a path allowlist                                      | Only a sandbox that reaches Anthropic through an injector needs one. The trusted worker calls Anthropic directly, and the guest reaches nothing.                                                          |
| A gRPC forwarding sidecar                                                                         | The forwarders run in the trusted process, on a socket the guest cannot leave.                                                                                                                            |
| A separate container running the SDK's worker                                                     | The session loop runs in the trusted process, with `shell` as the sandboxed replacement for `bash`.                                                                                                       |
| Custom ID-token audiences on the internal services                                                | With no load-balancer hostname in front of them, each service's audience is its own URL.                                                                                                                  |

The worker has ordinary egress: a NAT route to Anthropic, and a private path onto the services network for the
internal-ingress services. Only the *guest* has no network at all.

### The guest's world is assembled at build time

The guest's root filesystem is built into the image as a separate tree and bound read-only at launch, so the guest sees
none of the worker's userland, none of its dependencies and none of its credentials. Three surfaces make up everything
it can see: that read-only root, a writable `/workspace`, and a private `/tmp`. Nothing else is bound in.

Before serving anything the worker runs postern's self-test and refuses to start if isolation is not enforced on this
host. Off-platform — a developer's machine with no bubblewrap — that means the worker exits rather than running model
code unsandboxed, which is the behaviour we want from a boot gate: it fails to the safe side of a mistake in how it was
deployed.

One first-party library ships into that root beyond the interpreter and the gRPC runtime: the **guest SDK**, a small
module under a stable import name that hands out a stub accessor per exposed service over one shared channel to the
hatch. The working-document linter ships beside it, so the agent can check its own output without leaving the sandbox.

### The hatch is the capability boundary

The guest's one exit is a gRPC server on a Unix socket inside the sandbox. It admits exactly the methods on a generated
allowlist and answers everything else `PERMISSION_DENIED`; which methods those are is decided by a file option on the
`.proto` and generated from it, so the allowlist is never authored
([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)).

The worker registers one forwarding servicer per exposed service. Each injects the per-session token as request metadata
and forwards over a channel carrying the worker's own service identity. So the guest holds no credential it could
exfiltrate and knows no upstream address it could try to reach directly: it names a method, and the trusted side decides
what that means. One channel is held per upstream *deployment* rather than per service, since a deployment may serve
several interfaces ([`services.md`](services.md)).

### The data path is the trusted worker's

`/workspace` is restored once when the session starts and checkpointed back to the store repeatedly while it runs. Both
legs run in the trusted worker, directly against the store — there is no sync agent inside the sandbox — and every read,
write, pack and extract goes through postern's reference-closed accessor. That matters because the guest can plant
anything in `/workspace`: a document replaced by a symlink to the process environment is not followed out of the tree,
it fails to resolve.

A checkpoint is two writes: the working document, and a pack of the rest of the tree. Both hang on the return of a
sandboxed command — every `shell` call checkpoints when its command comes back — plus one final checkpoint after the
session loop returns. The unit one would rather have is the turn, and it is not available: the end-of-turn idle event is
consumed inside the SDK's loop, which arms its own idle clock on exactly that event while yielding us tool calls.
Reading the boundary would take a *second* subscription to the session's event stream, with reconnection, history
re-paging and de-duplication by event id, and the last command of a turn is strictly earlier than the boundary anyway.

What that cadence costs is the design's exposure, and is worth stating plainly. Between checkpoints the session's files
exist only in the container, and the SDK's own file tools store nothing: an edit a `write` or `edit` call made after the
session's last sandboxed command reaches the store only in the final checkpoint, and an ungraceful exit — an
out-of-memory kill, a preemption, the task timeout — loses every edit since the last command returned. The per-tool
deadline is the other exit no checkpoint survives: when it fires the SDK abandons the call, so the checkpoint that would
have followed the command never runs. The sandboxed command's own timeout is set below the SDK's, so a command that is
merely slow still returns in time — the gap is narrowed rather than closed.

Three failure policies fall out of what each half of the workspace is worth:

- **The working document is fail-closed.** Any store error on restore other than a definite "not there yet" fails the
  spawn. Booting onto a blank document and then serving a turn would mint a version over the curator's work.
- **The scratch is fail-open.** It is intermediate state the session can rebuild, so a failed restore leaves it empty
  and continues; the next checkpoint overwrites whatever was there.
- **A store error while checkpointing fails the command that triggered it.** The command's own work may well have
  succeeded and the model is told the call failed regardless — accepted, because a session that goes on advancing while
  nothing reaches the store loses more than one misreported call costs.

A document unchanged since its last write mints no new version, so a command that touched nothing does not inflate the
history; and the skills the SDK re-downloads each spawn are pruned from the pack, so no stale copy is checkpointed or
restored. Those skills do sit in writable `/workspace`, reachable by guest code — harmless, since the guest already runs
arbitrary code and they refresh every spawn, but a genuinely read-only mount would need postern to overlay a read-only
bind *inside* the workspace.

### The session's release lands about a minute after its last turn

The SDK's loop returns when the session terminates, or when its idle interval — a minute, by default — passes with no
new event following an end-of-turn idle. The final checkpoint is therefore written roughly a minute after the turn it
belongs to. A session the harness recorded as settled at 07:58:20 had its document in the bucket at 07:59:20.

A reader of the store has to expect that lag. What is stored is current as of the session's last sandboxed command; the
edits after it — the model's closing write to the working document, typically — arrive a minute after the conversation
goes quiet.

### Only arbitrary execution is sandboxed

The session's toolset is the SDK's standard file tools — read, write, edit, glob, grep — with `bash` dropped and a
`shell` tool in its place. The file tools resolve every path against the working directory and reject escapes, so they
run in the trusted worker; `shell` marshals its command into the guest. The agent's web search and fetch run on
Anthropic's side and touch neither the worker nor the sandbox.

`shell` marshals **every** command, with no fast path for anything. The command runs through postern's hatch-bound
Python entry point, which is what exports the hatch's socket address into the environment, so a `python3` the command
spawns inherits it and can reach the services in code mode. The tool also takes an `intent` — a short model-stated label
for the action — which the workbench renders and the worker logs beside the exit code, giving a worker-side audit of
what ran. No tool runs a model-supplied command on the host: one that did would bypass the sandbox entirely.

The trade-off is deliberate and worth stating plainly: the credential-holding process runs the file tools directly. It
is accepted because the toolset's own trust model holds the file tools safe unsandboxed and only `bash` unsafe, and
because the tools' path confinement is what that safety rests on.

### The work item is acked once restore proves

The dispatcher polls for work without acking, so an item stays reclaimable until someone takes responsibility for it.
The worker acks after `/workspace` restore succeeds and before serving. That ordering does two things at once: a spawn
that dies before restore stays unacked and correctly re-surfaces on a later drain, and a session that outlives the
reclaim window is not reclaimed and re-dispatched underneath itself.

A store error resolving the working document is terminal — a respawn would meet the same failure — so that item is acked
to stop reclaim and stopped to end it. It is the only item the worker stops itself. Once the session loop is entered the
SDK force-stops the item on every exit from it, shielded against cancellation, so a stop from the worker would only race
that one and take a conflict response the SDK tolerates and we do not.

The accepted limitation is on the other side of the ack: a worker death *after* the ack wedges the session. The item is
out of the reclaimable set, and the dispatcher drains only on a run-started webhook, which an idle waiting for action
never re-fires — so nothing re-drains it (§Open questions).

### The design is verifiable offline

The whole test suite runs without bubblewrap, against fakes at the narrowest seams: the store, and the SDK's event
stream. The properties only a real launch can prove — the boot gate passing, the guest having no network, the hatch
socket being the one channel bound in — are gated on the host having bubblewrap, so they run in CI and skip on a
developer's laptop rather than silently passing there.

## Alternatives considered

- **An untrusted agent container beside a trusted credential-proxy sidecar.** Weighed in §"One trusted process, not two
  containers": its cost is the network subsystem, and its benefit — running the SDK's worker exactly as shipped — is not
  worth a VPC.
- **An egress firewall around the sandbox instead of an empty namespace.** A firewall is fail-open: a wrong or missing
  rule permits. A namespace with no interface has no rule to get wrong. The firewall's one advantage, letting sandboxed
  code reach a curated set of external hosts, is not something this design wants — the agent reaches external data
  through services on the allowlist, which is where the curation belongs.
- **A shell-less, trimmed guest root filesystem.** Rejected as security theatre here: isolation rests on the empty
  namespace, the dropped capabilities and the non-root uid, all of which hold whatever binaries are present. Trimming
  would only cost the model the tools it is meant to use.

## Open questions

- **A wedged session has no automatic recovery.** A worker death after the ack leaves nothing to re-drain the item
  (§"The work item is acked once restore proves"). Recovering it would need a reclaim tick independent of the webhook.
- **The checkpoint caps are asymmetric.** Restore caps entries and total bytes; the pack side caps nothing, so a guest
  can inflate the archive it ships — bounded to one execution, and the store caps its own blob, but the pack side should
  be capped too.
- **Memory is bounded from outside, not by a cgroup.** The container's own memory limit is the only bound, giving a
  one-session blast radius; postern's per-process address-space cap is left unset, and a delegated child cgroup would be
  the better answer if the platform allows one. The per-run process-count cap postern applies by default is untested.
- **Where the worker's code belongs.** It sits under `themis/services/`, while
  [`../repo-structure.md`](../repo-structure.md) reserves an `apps/` path for the eventual deployable and defines
  `themis/services/` as gRPC servicers — and the worker is not a servicer. To reconcile.
