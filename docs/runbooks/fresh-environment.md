# Runbook: bring up a fresh environment

Stand up a Themis environment (`dev` now, `prod` later) from nothing. Two tiers: a one-time `bootstrap.sh` (the
substrate Pulumi depends on), then `pulumi up` (everything else). Design:
[`spike-infrastructure.md`](../design/spike-infrastructure.md), [`deployment.md`](../design/deployment.md). Program
layout: [`infra/README.md`](../../infra/README.md).

Examples below use `dev` / `cpg-themis-dev`; for prod substitute `prod` / `cpg-themis-prod` and run the same steps — no
program change.

## Prerequisites

- The GCP project exists with billing linked (provisioned by central infra; the spike's `cpg-themis-dev` already carries
  a budget).
- You have **Owner** on the project (to run `bootstrap.sh`) and local `gcloud` ADC:
  `gcloud auth application-default login`.
- The IAP access group exists — see [`iap-access.md`](iap-access.md). It must exist before `pulumi up` (the IAP IAM
  binding targets it).
- This environment's own programmatic Desktop OAuth client exists in the Console — a fresh one, never another
  environment's — and its id is in `Pulumi.<stack>.yaml` as `themis:iapProgrammaticClients`; see
  [`iap-access.md`](iap-access.md). The allowlist is required, and an environment that admits no programmatic client yet
  declares it as `[]`, which is what makes the program leave the backend's IAP settings undeclared. A stand-in client id
  is not the inert value: it would be written into those settings for real. The paired secret is an encrypted write, so
  it cannot be set until the stack exists — §2 does it.

### Local-operator KMS access (named individuals)

Local `pulumi` runs under the operator's own ADC and must decrypt/encrypt the gcpkms secrets key — including at
`stack init`. Owner does **not** grant KMS crypto operations, so each operator who runs Pulumi locally is granted
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key by name (the deploy SA gets it from bootstrap; the preview SA
gets decrypt only):

```sh
gcloud kms keys add-iam-policy-binding pulumi \
  --project=cpg-themis-dev --location=australia-southeast1 --keyring=themis \
  --member="user:<operator>@populationgenomics.org.au" \
  --role=roles/cloudkms.cryptoKeyEncrypterDecrypter
```

This is the only standing human grant on the key; revoke it to off-board an operator. Decrypts are audit-logged.

## 1. Bootstrap (once per environment)

```sh
PROJECT=cpg-themis-dev infra/bootstrap/bootstrap.sh
```

Creates: the per-environment state bucket, the KMS key, the GitHub WIF pool + `themis-deploy` (write, deployable refs) /
`themis-preview` (read-only, PRs) service accounts, and network hardening (drops the default VPC + its permissive
rules). Idempotent. The deploy/preview SA emails and the WIF provider path it prints are already wired into
`.github/workflows/{deploy,preview}.yml`.

## 2. First bring-up (once per environment)

The registry is created by the program, so the first `pulumi up` uses a public placeholder image — that one `up` creates
the registry *and* brings the edge up running the placeholder; later deploys push real images to that registry.

Two of the required keys name values this same `up` produces, so they carry placeholders for this one run —
[§3](#3-values-that-only-exist-after-the-first-up). Every other key the program reads must already hold its real value,
including the allowlist from Prerequisites: `preview` stops at the first `config.require*` the stack does not satisfy,
before anything is created. The client secret is not one of them — the program never reads it, so a missing one is
silent here and surfaces at the first `login`.

```sh
cd infra
pulumi login gs://cpg-themis-dev-pulumi-state
# Pass the secrets provider explicitly at init — `stack init` does not read it
# from the committed Pulumi.dev.yaml; without it Pulumi falls back to passphrase.
pulumi stack init dev \
  --secrets-provider="gcpkms://projects/cpg-themis-dev/locations/australia-southeast1/keyRings/themis/cryptoKeys/pulumi"
# Encrypted to the stack's key, so it waits for the init above. No `up` reads it,
# so a stack that never gets it deploys clean and fails at the first `login`.
pulumi config set --secret themis:iapProgrammaticClientSecret <secret-from-the-console>
THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi preview   # review the plan
THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi up
```

`stack init` writes a generated `encryptedkey` line into `Pulumi.dev.yaml`; commit it (inert without KMS access, safe on
the mirror). Requires the operator KMS grant from the prerequisites above.

## 3. Values that only exist after the first `up`

Some of what the program requires, the program itself produces — circular by construction, so a fresh environment cannot
declare it up front. The first `up` runs against placeholders; each real value is then read from a stack output and set,
and a second `up` applies it. Whatever each one feeds stays inert until then, which costs nothing on a first bring-up:
the edge is still serving the placeholder image.

| Key                                | Real value                                                                                                                                    | Inert until set                                                                                        |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `themis:iapBackendServiceId`       | `pulumi stack output web_backend_service_id`                                                                                                  | The web app verifies the IAP-JWT audience against this id, so it refuses every assertion IAP hands it. |
| `themis:anthropicFederationRuleId` | The rule registered in the Anthropic console against `pulumi stack output web_sa_unique_id` — [`claude-api-wif.md`](claude-api-wif.md) Path B | The BFF cannot mint a Managed-Agents token.                                                            |

Any non-empty string serves as the placeholder; neither the program nor the app parses these beyond requiring them. The
remaining `themis:anthropic*` ids are Anthropic-side entities that exist before the GCP service account does, so they
are set for real from the start.

```sh
pulumi config set themis:iapBackendServiceId "$(pulumi stack output web_backend_service_id)"
pulumi config set themis:anthropicFederationRuleId fdrl_...   # from the Anthropic console
pulumi up
```

Never copy either value from another environment's `Pulumi.<stack>.yaml`. A stale `iapBackendServiceId` points the app
at another environment's backend, so it refuses the traffic its own IAP admits — a failure that reads as a broken deploy
rather than a wrong constant.

### DNS handoff (external — IT team)

```sh
pulumi stack output lb_ip
```

Give that IP to the IT team and ask for an **A record** `themis-dev.populationgenomics.org.au → <lb_ip>` (an A record
points a name at an IP; a CNAME can't). The Google-managed TLS certificate stays `PROVISIONING` until the record
resolves, then goes `ACTIVE` (minutes to ~an hour). The IP is `protect`ed and stable, so it's safe to hand out before
the cert is live.

## 4. Hand off to CI

Once bootstrap + the first bring-up are done, CI owns deploys: PRs get a read-only preview comment; `deploy` builds the
images and `pulumi up`s when `deployed/<env>` is pushed, or when dispatched on `main`. Merging does not deploy
([`../design/deployment.md`](../design/deployment.md)).

### Enabling the ad-hoc deploy branch

Optional, and dev-only — `prod` should deploy from `main` alone. The steps are ordered: the WIF member is the credential
and the ruleset is the only thing in front of it, so granting the member first opens a window in which any repo write
can take the deploy SA ([`../design/deployment.md`](../design/deployment.md)).

1. **Confirm the branch does not exist yet.** `git ls-remote origin 'refs/heads/deployed/*'` must print nothing. A ref
   created before the ruleset is not retroactively covered by it — `Restrict creations` only blocks future creations, so
   an existing branch is simply frozen with whatever it already points at. If anything comes back, find out who pushed
   it before going further.
1. **Create the ruleset**, targeting `deployed/*` — rules `Restrict creations`, `Restrict updates`,
   `Restrict deletions`; bypass list: the **repository-admin role**, a role rather than named people. Bypass covers the
   whole ruleset, so admins keep the force-push that repointing the branch needs while everyone else cannot even create
   it. Do **not** add `Require a pull request before merging`: it would let a non-admin land content through a PR that
   `Restrict updates` would otherwise refuse.
1. **Check the ruleset actually matches.** The ruleset UI lists matching branches; a pattern that silently matches
   nothing looks configured and enforces nothing. Confirm a non-admin can neither push nor merge into `deployed/dev`.
1. **Only then grant the WIF member:**
   ```sh
   ADHOC_DEPLOY_BRANCH=deployed/dev PROJECT=cpg-themis-dev infra/bootstrap/bootstrap.sh
   ```

### Disabling it again

Re-running `bootstrap.sh` without `ADHOC_DEPLOY_BRANCH` does **not** revoke the member — the script only adds bindings,
so that an unrelated rerun cannot silently cut off a deploy path someone is using. Withdrawing it is deliberate and
manual, in this order (member first: while the member exists, the ruleset is the only thing holding the branch):

```sh
gcloud iam service-accounts remove-iam-policy-binding \
  themis-deploy@cpg-themis-dev.iam.gserviceaccount.com \
  --project=cpg-themis-dev --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/github/attribute.ref/refs/heads/deployed/dev"
git push origin --delete deployed/dev
```

Leave the ruleset in place: it costs nothing and keeps the branch un-creatable, which is the safer residue.

## Tearing down

The reserved IP is `protect`ed; `pulumi destroy` refuses until you clear the protection (deliberate — it guards the
externally-bound address).
