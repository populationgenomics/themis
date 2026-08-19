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
impersonates `themis-clu` and presents a token, rather than holding a credential of its own. The account is granted
`roles/iap.httpsResourceAccessor` on the backend alongside the access group, so IAP admits it as it admits a person.

Nothing has to be registered for it. IAP accepts two token types from a service account, and they differ in what the
audience is:

- A **self-signed JWT** — the account signs it, `aud` is the app's own URL. This is what Themis uses. No OAuth client,
  no allowlist, no consent flow, no secret and no cached refresh token.
- An **OIDC ID token** — Google issues it, and `aud` must be an OAuth client id registered against the resource in
  `programmaticClients`. Themis registers none, so this type is refused here.

The second is the one whose audience cannot be derived from the deployment: the client IAP itself runs on is
Google-managed and shared across every tenant, so it reports no id of its own and none can be borrowed. That constrains
the OIDC type only; it says nothing about the JWT type, whose audience is the URL you are calling.

The token shape and what the app sees as the caller's identity are in
[`hand-driving-a-service.md`](hand-driving-a-service.md), with the services and the database.

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
