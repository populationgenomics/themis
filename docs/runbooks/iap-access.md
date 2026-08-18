# Runbook: IAP access

Who can reach a Themis environment's web app is a coarse IAP gate: the load balancer's backend has IAP enabled, and a
single **per-environment Google Group** is granted `roles/iap.httpsResourceAccessor` on it. Application roles and
per-report authorization live in the app, not here.

## The group principal

| Environment | Group                                          |
| ----------- | ---------------------------------------------- |
| dev         | `themis-dev-access@populationgenomics.org.au`  |
| prod        | `themis-prod-access@populationgenomics.org.au` |

This repo references **only the principal** (`themis:iapAccessGroup` in `infra/Pulumi.<stack>.yaml`) — a single non-PII
identifier, safe on the public mirror. The group and its **roster never appear here**: a member list is PII.

## Where membership lives, and how to add someone

The group and its members are managed by PR in **`cpg-infrastructure-private`** (CPG's existing Cloud Identity machinery
— `gcp.cloudidentity.Group` + per-group `members` in `groups.yaml`, mapped through `users.yaml`). To add or remove
access:

1. PR `cpg-infrastructure-private`: add the username to the group's `members` in `groups.yaml` (and a `users.yaml` entry
   if the person is new there).
1. Merge → its deploy pipeline applies the membership.

No change in this repo. The group must **exist before** the environment's first `pulumi up` (the IAP IAM binding targets
the principal).

## Programmatic access

Local automation (a deploy smoke check, an evaluation harness) reaches the app as the developer running it, through
`themis.clients.iap`.

### The three gates

Reaching an answer needs all three. The first two are declared in `infra/`; the third is not:

1. **IAP admits the token's audience.** IAP accepts an ID token only when its `aud` is an OAuth client on
   `accessSettings.oauthSettings.programmaticClients`, set per environment by `themis:iapProgrammaticClients` in
   `infra/Pulumi.<stack>.yaml`. Without it, IAP answers
   `Invalid IAP credentials: Invalid bearer token. Invalid JWT audience.`
1. **IAP admits the identity.** `roles/iap.httpsResourceAccessor`, held only through the access group above.
1. **The app admits the identity again.** Every named project and analysis is gated on a `project_members` row for the
   caller's email. Nothing in this repo writes that table; it has no application writer at all.

The effective access radius is therefore the IAP group **intersected with** `project_members` — not the group. Nothing
declares that intersection in one place: gate 3 is a table with no application writer, so a developer who clears IAP can
still be answered as though the environment were empty. A non-member is answered **404, never 403**, so a caller cannot
learn that an analysis outside their projects exists; "path does not exist" and "you are not a member" are
indistinguishable from outside.

### Why a dedicated client, not gcloud's

The allowlist is scoped to a *client*, and that is the only one of the three gates that can separate interactive from
programmatic access. Curators sit in the access group and hold `project_members` rows — both legitimate, both needed for
browser access — so neither IAP's IAM check nor the app's own gate distinguishes a curator from a developer.
Allowlisting gcloud's shared client id would turn every curator's ordinary cached gcloud credential into a programmatic
key to the app. Themis therefore allowlists a **dedicated Desktop OAuth client**: reaching the app programmatically
means consenting to *that* client, which a curator has no reason or means to do.

Be precise about what bounds this. A Desktop client's secret is not a confidentiality boundary — RFC 8252 §8.5 and
Google both treat an installed-app secret as non-secret, since it ships inside anything that uses it. What actually
bounds the path is the **Internal consent screen** (only org identities can consent at all) stacked on the two IAP gates
and `project_members`. Keeping the secret in KMS-encrypted config raises the bar and matches the developer/curator line;
it is not what enforces it.

An environment that lists no clients declares no IAP settings resource, and its IAP settings stay unmanaged; an
out-of-band `gcloud iap settings set` there would not surface as drift. `Pulumi.prod.yaml` must set
`themis:iapProgrammaticClients` explicitly — there is no default.

Emptying a populated list revokes: dropping the resource destroys it under `deletion_policy=DELETE`, which PATCHes the
backend's IAP settings empty rather than only forgetting them, so the allowlist does not survive in GCP. Going from
empty to empty is the unmanaged case above — nothing was declared, so nothing is cleared.

### The client, and where its secret lives

A Desktop-type OAuth client under an **Internal** consent screen, created in the Console. Its id is public and sits in
plaintext in `infra/Pulumi.<stack>.yaml`; its secret is `themis:iapProgrammaticClientSecret`, encrypted in the same file
under the stack's KMS key.

**One client per environment**, never a shared one. The client is a project-scoped resource, and its id is what gate 1
admits: allowlisting dev's client on prod would have prod trust a credential dev issues, and a dev consent — whose cache
holds dev's id *and* secret — would mint tokens prod's gate admits. It is also what makes a consent environment-specific
at all, which the per-run section below relies on.

Pulumi config is not a runtime secret store, and no service reads this value — it lives there because the stack's KMS
key already gates decryption to the people who deploy the environment. That the developer/curator split holds is an
operational precondition of the KMS key's IAM, which is granted out of band, not by this repo.

**Rotation.** Rotate in the Console, then `pulumi config set --secret themis:iapProgrammaticClientSecret <value>` and
commit the rewritten `secure:` line in `Pulumi.<stack>.yaml` — an uncommitted rotation leaves the next operator
decrypting the old value. No `pulumi up`: nothing in the program reads this key. If the Console issued a *new client*
rather than a new secret on the existing one, the id changed too — set `themis:iapProgrammaticClients` and `pulumi up`
as well, because until the new id reaches the allowlist IAP refuses every token minted against it.

Every developer's cached consent is stale from that moment and surfaces as a refused refresh token; each re-runs the
setup below. Revoking a *single* developer's access is not this lever — remove them from the access group.

### One-time setup

```bash
uv run --group iap python -m themis.clients.iap login
```

`login` reads the client out of the environment's Pulumi stack config itself: the id from
`themis:iapProgrammaticClients` (a list — it must name exactly one client, or the run refuses rather than pick), the
secret from `themis:iapProgrammaticClientSecret`, decrypted through the stack's KMS key. So it needs the `pulumi` CLI, a
`pulumi login` to that environment's state bucket, and decrypt access on the key — the same access that keeps
programmatic reach on the developer side of the line. `--stack` selects the environment (default `$PULUMI_STACK`, else
`dev`), and the run reports where each half of the client came from: a consent against the wrong environment otherwise
surfaces only much later, as IAP refusing the token.

`THEMIS_IAP_CLIENT_ID` and `THEMIS_IAP_CLIENT_SECRET` override either half, for a machine that cannot reach Pulumi. Each
resolves on its own, so setting one still reads the other from the stack — and an override outranks `--stack`, which is
why the run reports both halves rather than only the environment it read.

The consent itself opens a browser for Google's consent screen and receives the authorization code on an ephemeral
loopback port (out-of-band redirection is not used; Google withdrew support for it). It then spends the grant once, so a
consent that cannot be replayed is caught now rather than on the next run — it reaches only Google, so whether IAP
admits the client is settled by the first real request, not here.

The refresh token and the client secret are cached together at `$XDG_CONFIG_HOME/themis/iap.json` (`~/.config` when
unset), mode `0600` — neither half authenticates without the other, so splitting them would protect nothing.
`$THEMIS_IAP_CREDENTIALS` overrides the location. Nothing reads stack config again after this; `token` spends the cache.

One cache holds one consent, so which environment it is for is settled at `login`: reaching a second one means
consenting again, or pointing `$THEMIS_IAP_CREDENTIALS` at a second file. `token` therefore takes no `--stack` — it
would name an environment the cache cannot honour.

### Per run

```bash
curl -H "Authorization: Bearer $(uv run --group iap python -m themis.clients.iap token)" \
     -H "content-type: application/json" -d '{}' \
     https://<hostname>/api/rpc/themis.workbench.rpc.Workbench/ListProjects
```

The BFF's surface is Connect (`docs/design/proto.md`): one `POST` per method at `/api/rpc/{package}.{Service}/{Method}`,
request and reply both proto3-JSON. No Connect method answers a `GET`, reads included, so the `curl` above is the way in
to any of them.

Each invocation exchanges the refresh token for a fresh ID token. Because the allowlisted client is our own, the token
Google issues already carries `aud = <our client id>` — the audience IAP wants — so there is no second client to swap
audiences against.

From Python, `themis.clients.iap.client.from_cache(base_url)` returns a client that re-mints as its token nears expiry
and translates each of the three refusals into what to go and do about it.

## OAuth consent (one-time, per project)

The **browser** path uses a Google-managed OAuth client — nothing to create or store for it (`spike-infrastructure.md`
§4). The project still needs an OAuth consent screen (brand), configured once. The programmatic Desktop client above
hangs off that same screen and is the only OAuth client this project owns.

The screen's user type is load-bearing twice over, and the two pull in opposite directions: **Internal** is what bounds
programmatic access to org identities (see above), but it also refuses consent to out-of-org accounts — which the next
section expects to exist. Confirm the current user type before adding an external member, and treat a change to it as a
change to the access model, not a console detail.

## External members

The group may contain non-CPG Google accounts (curators are often external), so IAP access is **not** org-domain-gated.
External *membership* is a separate Workspace setting and may be constrained by the `iam.allowedPolicyMemberDomains` org
policy — confirm with central infra before adding external members. All current dev members are in-domain, so this isn't
blocking yet.
