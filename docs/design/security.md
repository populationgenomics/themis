# Design: security architecture

**Status:** current **Related:** [`frontend-framework.md`](frontend-framework.md) (§Auth — the web request-auth
chokepoint), [`sandbox-worker.md`](sandbox-worker.md) (the hatch the sandboxed agent's calls leave through),
[`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) (the condition an rpc meets before the agent may call it),
[`workspace-model.md`](workspace-model.md) (cross-Project default-deny), [`../PRODUCT.md`](../PRODUCT.md) §9 (security
posture this implements)

## Overview

Cross-cutting security rules, distinct from the per-area docs that own each mechanism. Two, each stated once here rather
than re-argued per area:

- **Every critical security check runs at a single default-on chokepoint** — never as a step each call site opts into.
- **An outbound request carrying untrusted text is an exfiltration channel when the untrusted side can influence where
  the bytes go, or can read them back later** — not merely because untrusted text crossed the perimeter.

## Background

A security check that each call site must remember to invoke is a latent hole. The default is *unprotected*, and an
omission — a new route, a new tool, a new egress path — is indistinguishable from a deliberate exemption. The system is
only as safe as its least careful addition, and that addition is reviewed by someone who has to notice an *absent* line.
Request auth, egress restriction, data-boundary enforcement, and sandbox tool-gating all share this shape.

The second rule answers a different problem: a test that fires too often stops discriminating. The analysis agent is
untrusted code reading untrusted text — published papers, third-party records — and the evidence rpcs it calls reach
public databases on its behalf. That outbound hop is not incidental to the product; a variant lookup *is* a request to
NCBI or VariantValidator with the caller's identifier in it. If every such hop counted as the external-channel leg of
the lethal trifecta ([`../PRODUCT.md`](../PRODUCT.md) §9), the trifecta would close on the product's first feature, and
"this is an exfiltration channel" would carry no information. What has to be decided, once, is which hops an attacker
can actually get data out through.

## Design

### One default-on chokepoint

A critical security check MUST be a single chokepoint that is **enforced by default**, with exemptions **explicitly
allowlisted** in one place.

- **Default-deny.** Absence of an explicit decision denies. A new code path inherits the check for free; exempting it is
  a visible, reviewable edit to the allowlist — not a matter of remembering to add a line. The reviewer reads a present
  entry, never has to spot a missing one.
- **One implementation.** The check's logic lives once and is shared. Call sites neither re-implement nor re-decide it.
- **At the resource, not only the perimeter.** The authoritative check sits where the protected resource is actually
  accessed. A perimeter gate (an HTTP proxy, an API gateway) is defense-in-depth *on top*, never the sole authority: a
  framework bug (e.g. Next.js CVE-2025-29927 skipped middleware via a request header) or a routing change that silently
  drops coverage can bypass a perimeter while the resource-side check still holds.
- **Fail loud.** A check that cannot run — missing config, unverifiable input — denies and raises. It never degrades to
  allow ([`../style/general.md`](../style/general.md) "Fail loud").

"If at all possible" is a real qualifier. Where a language or framework offers no default-on seam, the fallback is the
narrowest shared wrapper *plus* a lint/CI rule that fails when a call site skips it — the allowlist enforced
mechanically, not by convention. A per-call-site check guarded only by reviewer discipline does not satisfy this rule.

### What counts as an exfiltration channel

An outbound request that untrusted text reaches is an exfiltration channel unless both of these hold:

- **Destination-fixed.** Scheme, host, port and route come from code constants, and no parameter is one the upstream
  itself dereferences as a location — a `url=` it fetches, a callback it calls, a redirect target it hands back. The
  untrusted text appears only as a percent-encoded parameter value, a path segment or a request body, so it selects what
  comes *back* and never where the request *goes* — a body cannot steer a URL assembled from constants, which is why our
  gnomAD GraphQL query and our ClinVar batch fetch are inside this and not exceptions to it. That second half cannot be
  read off our call site: a location parameter leaves our own URL constant and the call a read, so both legs look
  satisfied here while the upstream carries the text on to a host the attacker named — the same shape as a URL built
  from fetched data, one hop out.
- **Query-only.** The call's effect is to select a response. A call that *deposits* the text — a submission, a comment,
  an upload, anything the upstream stores and later serves to someone else — is a publication channel, because the
  reader on the far side can be whoever planted the text.

The two legs meet at the route. *Query-only* is asserted of an endpoint — this route on this host only answers — so a
caller who can steer the route leaves the assertion nothing to attach to. Unencoded text in a path segment does exactly
that: a `/` or a `../` walks onto a sibling route, and where the call carries no query parameters of its own, everything
after a `?` is the caller's as well. A host that answers reads on one route and takes a submission on another is then a
publication channel whose destination never moved, and the verb does not deny it (below).

The query has the same failure, one step further on. Unencoded text in a query string assembled by hand lets the caller
*add* a parameter rather than fill one — and the parameter worth adding is the location parameter the first leg bars us
from passing, so the attacker supplies from inside the value what our code was careful never to name. We do not assemble
query strings: every outbound call hands its parameters to the HTTP client, which encodes them, so an `&` in a search
term stays inside that term's value instead of starting a parameter of its own. That is an invariant to keep rather than
a defect to fix, and it is the reason percent-encoding the caller's text is not hygiene about which record comes back —
it is what holds both legs up.

Where both hold, an attacker who has smuggled instructions into evidence text can make our service ask a public database
an arbitrary question, and learns nothing: the bytes land in NCBI's request log, which they cannot read. A destination
of their own is what they need, and destination-fixedness is what denies it.

That last clause is the criterion's premise rather than an aside, and it is worth being exact about what it assumes.
Assume every request we make is logged and kept somewhere. What the premise does *not* rest on is the operator: not that
they are careful with our queries, and not that they are large enough to lose something by mishandling them. A public
archive is under no duty to screen what it is sent — if we put a secret in a submission it publishes, that is our doing
and not a lapse of theirs, and an upstream's standing bounds nothing. The assumption is mechanical, and it is two facts
about a named host: it answers what we ask and keeps no copy it serves on to someone else, and it has no parameter that
sends the request somewhere else. Neither is visible from our call site, which is why each is recorded per host in
[`destinations.py`](../../themis/services/evidence/upstreams/destinations.py) rather than left to whoever writes the
next adapter. So the free-text identifier such an rpc takes — a gene symbol, an HGVS descriptor, a search term — is
bounded by *where* it can be put, not by how long it is or what it looks like.

Three things the criterion deliberately does not rest on:

- **Not the HTTP verb.** Query-only is a property of the upstream operation, not of the method: our gnomAD adapter's
  GraphQL query and our ClinVar batch fetch are both POSTs that only read, and a GET against an endpoint that records
  what it was asked would be a deposit. This leg is therefore asserted by whoever exposes the rpc, from what the
  upstream does with the request — it cannot be read off the call site.
- **Not a size cap or a rate limit.** A secret fits in a short string and a single request, so neither bounds what
  leaves in a request's content; offering one as the remediation for an exfiltration finding mistakes cost for
  confidentiality. The exception is the channel that puts nothing in the request at all — the third residual below,
  where a request budget is the only bound there is. Otherwise both are controls for cost and abuse, which the exposure
  condition covers on its own terms ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) §Security).
- **Not a closed value set.** Where a parameter's domain is finite, a constrained type is the better interface and
  [`../PRODUCT.md`](../PRODUCT.md) §9 asks for it. It is not what confines this channel: with the destination fixed, an
  accepted value and a rejected one leak the same nothing, and most of these parameters have no finite domain anyway (an
  HGVS descriptor, a literature query). Validating a gene symbol against HGNC would buy clearer errors, at the price of
  a symbol list that is stale the day a gene is renamed — a trade worth arguing on those merits, not as a leak control.

Three residuals, which are the reason this is a criterion applied per rpc and not a blanket exemption: two are
properties of the upstream, and the third carries nothing in the request at all. The upstream sees the text, so anyone
who can read the upstream's view of it — its logs, or a query it stores and serves back — holds a channel; that is a
property of the upstream, which is why *query-only* is asserted about a named upstream rather than assumed of all of
them. And a destination that is fixed today stops being fixed the moment a URL is built from fetched data rather than
from a constant: a host read out of a record, a link followed out of a search result, a redirect chased. That shape *is*
a channel, and catching it is what the criterion is for.

The third carries nothing in the request. *Whether* we ask, and *when*, is visible to the upstream and to anyone reading
its logs, so an attacker who can steer what the agent decides to look up can signal in the timing and count of our
requests alone — a few bits at a time, with nothing in any request that the two legs above inspect. Nothing here bounds
it and nothing cheaply can, because the traffic is indistinguishable from work. What would bound it is a budget on how
many requests one session may make — the exposure condition's cost clause doing confidentiality work as a side effect —
and no such budget exists: the exposed rpcs bound their own latency, not their rate. What exists instead is the record:
every upstream call the shared client completes is logged with the URL asked, and the environment keeps its logs — these
and every other, the window is the project's — for as long as it keeps its audit logs, so a pattern that looks like
signalling is answerable after the fact even though nothing stops it in the moment. That record holds the caller's
identifiers — the gene symbol, descriptor or coordinate a case was looked up by — and its readers are whoever holds
log-viewer on the project. That set is already the set who can read the case itself, so the record widens no one's
reach; the entry would need its own bucket in an environment where the two came apart. We accept the residual on those
terms — detection rather than prevention, while the bandwidth stays this low. It is named so that a wider version of the
same shape is recognised rather than rediscovered.

Destination-fixedness is a property of code, and by the chokepoint rule above, a property each call site holds by
remembering to hold it is not held. It splits in two, and only one half is held today.

*Which host* is reachable is a chokepoint for everything riding the evidence image's shared client, which is every live
upstream call it makes: the client is built with the admitted set, so a request to a host with no determination raises
before it leaves, at the request rather than at a perimeter. Adding an upstream is an edit to
[`destinations.py`](../../themis/services/evidence/upstreams/destinations.py) that a reviewer reads. What keeps calls on
that client, though, is the shape of the adapters — each is handed it rather than building one — and not a rule that
fires when someone builds their own, which is the weaker form this section warns about. Two clients are already built
elsewhere, both deliberately outside the set and neither carrying caller text: the gene-disease refresh job, whose URLs
are constants and which follows redirects the admitted set could not allow, and the litcache ingest.

*Where the text lands inside that host's URL* is not. Each adapter percent-encodes its own path segments and hands its
own parameters to the client, adapter by adapter, and one unencoded interpolation is all it takes to lose the route.
Making that half structural too is the open question below.

The ordering this suggests is worth stating, because it is already how the gene-disease sources work: a hop you do not
make cannot leak. Where a source publishes in bulk, a periodic refresh into our own bucket removes the channel outright
— ClinGen's validity and dosage tables, GenCC and PanelApp are read from that copy, so no caller's text reaches those
hosts at all. Only what cannot be mirrored is queried live, against the admitted set.

### Where the rules land

- **Web request auth** — a proxy default-deny perimeter plus a shared, request-scoped accessor that re-verifies the IAP
  assertion at the data seam; `healthz` is the sole allowlisted exemption, reached directly by the Cloud Run probe
  rather than through the load balancer. The data-seam check is an interceptor on the RPC router, so it covers every
  method by construction. Owned by [`frontend-framework.md`](frontend-framework.md) §Auth.
- **Sandbox egress** — the guest process has no network at all; its one exit is the hatch, whose method allowlist is
  generated from the proto rather than authored. Owned by [`sandbox-worker.md`](sandbox-worker.md), with the exposure
  condition an rpc must meet in [`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) §Security — the place the
  exfiltration criterion is applied, since a forwarded rpc is how anything the agent says reaches a third party.
- **Service egress** — the internal services' VPC is deliberately not sealed, so what an exposed rpc dials is bounded by
  its own code and not by a network policy. The evidence image's shared HTTP client is that bound: it admits only the
  hosts [`destinations.py`](../../themis/services/evidence/upstreams/destinations.py) records a determination for.
- **Cross-Project data** — default-deny sharing; case-level content never crosses Project boundaries implicitly. Owned
  by [`workspace-model.md`](workspace-model.md) and [`../PRODUCT.md`](../PRODUCT.md) §7.

## Alternatives considered

- **Per-call-site enforcement** (each route/tool invokes the check itself). Rejected: opt-in, so the default is
  unprotected and an omission reads the same as an intentional exemption — the Background failure mode.
- **Perimeter-only enforcement** (one gate in front, nothing at the resource). Rejected as the *sole* layer: bypassable
  by a framework bug (CVE-2025-29927) or a routing/matcher change that drops coverage unnoticed. It is a valid added
  layer, not the authority.
- **Any outbound hop carrying untrusted text is a channel.** Rejected: it condemns the evidence lookups the product is
  made of, and a rule that fires on every feature is one reviewers learn to wave through — the finding stops being read.
  Worse, it hides the shape that matters, since a data-derived destination and a constant one score alike.
- **Bounding the text instead of the destination** (length caps, character classes on free-text identifiers). Rejected
  as a leak control for the reason given above: a secret is short. Kept where it earns its place as input validation — a
  bounded numeric run in an accession pattern is there so a malformed accession fails our parse, not so it fails to fill
  a URL.
- **An egress allowlist in front of the services** (proxy or firewall confining which hosts an internal service may
  dial). Not rejected — a real second layer, and the one that would survive an adapter losing destination-fixedness. It
  is not the authority: the request is legitimate to an allowlisted host either way, so the code-side property is what
  decides. Worth its own design when the upstream set stops being a handful of public databases.

## Open questions

- What makes destination-fixedness structural rather than per-adapter, in either half. Which host is reachable is bound
  by the shared client, but nothing fails when a caller builds its own; where the text lands inside that host's URL is
  each adapter's own encoding. One lint over client construction would close the first, and the candidate for the second
  is one request builder the adapters compose through — constant base, path segments and parameters as arguments,
  encoding applied by it — so that interpolating untrusted text into a URL is not something an adapter can express.
  Whether that is worth its churn across the existing adapters, and whether a lint rule over URL construction is the
  cheaper half, is undecided.
