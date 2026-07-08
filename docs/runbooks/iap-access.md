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

## OAuth consent (one-time, per project)

IAP uses a **Google-managed OAuth client** — no client secret is created or stored (`spike-infrastructure.md` §4). The
project still needs an OAuth consent screen (brand) configured once; confirm with central infra whether it exists for
the project or needs creating.

## External members

The group may contain non-CPG Google accounts (curators are often external), so IAP access is **not** org-domain-gated.
External *membership* is a separate Workspace setting and may be constrained by the `iam.allowedPolicyMemberDomains` org
policy — confirm with central infra before adding external members. All current dev members are in-domain, so this isn't
blocking yet.
