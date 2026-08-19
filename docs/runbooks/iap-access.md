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

Reaching the app without a browser goes through the same account as every other deployed surface: a person or a script
impersonates `themis-clu` and mints an ID token, rather than holding a credential of its own. The account is granted
`roles/iap.httpsResourceAccessor` on the backend alongside the access group, so IAP admits it as it admits a person.

There is no consent flow, no client secret and no cached refresh token. There *is* an OAuth client id, and it is
load-bearing: IAP admits a programmatic caller only when the token's audience is a client id registered against this
resource, in `themis:iapProgrammaticClients`. The client IAP itself runs on is Google-managed and shared across every
tenant, so it reports no id that can be used — a registered id is the only address IAP answers to here. The id is
public, an address rather than a credential; a token bearing it still needs the accessor role or IAP answers 403.

The token shape and what the app sees as the caller's identity are in
[`hand-driving-a-service.md`](hand-driving-a-service.md), with the services and the database.

## The registered client, and what was retired

The registered id belongs to a Desktop OAuth client. It once carried programmatic access through a browser consent flow,
with its secret in stack config and a per-developer cache of `client_id`, `client_secret` and `refresh_token`. The flow
is gone — impersonation supplies the identity now — but **the client must survive**: it is the audience, and deleting it
in the Console takes programmatic access down with it (every candidate audience IAP will admit is enumerated in
[`hand-driving-a-service.md`](hand-driving-a-service.md); there is no substitute).

What is left to clean up is the credential half, which nothing uses:

- Rotate the client's secret in the Console (APIs & Services → Credentials). That invalidates every cached refresh token
  without touching the id, so the audience keeps working.
- Each developer removes their cache: `rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/themis/iap.json"` (or whatever
  `$THEMIS_IAP_CREDENTIALS` pointed at).

Until both are done the secret sits on disks with no use for it.

## The OAuth consent screen

The browser path uses a Google-managed OAuth client, so there is nothing to create or store for it
(`spike-infrastructure.md` §4). The project still needs an OAuth consent screen (brand), configured once in the Console;
`infra/` declares no `gcp.iap.Brand`, so it is a manual prerequisite of a fresh environment rather than something a
`pulumi up` creates.

Its user type is load-bearing: **Internal** bounds access to org identities, but it also refuses consent to out-of-org
accounts — which the next section expects to exist. Confirm the current user type before adding an external member, and
treat a change to it as a change to the access model rather than a console detail.

## External members

The group may contain non-CPG Google accounts (curators are often external), so IAP access is **not** org-domain-gated.
External *membership* is a separate Workspace setting and may be constrained by the `iam.allowedPolicyMemberDomains` org
policy — confirm with central infra before adding external members. All current dev members are in-domain, so this isn't
blocking yet.
