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
- The project has an OAuth consent screen (brand), configured once in the Console — `infra/` declares no
  `gcp.iap.Brand`, so no `up` creates it; see [`iap-access.md`](iap-access.md), whose user type also decides whether
  out-of-org accounts can be admitted at all.
- The `themis-clu` group exists — see [`hand-driving-a-service.md`](hand-driving-a-service.md). It must exist before
  `pulumi up` too: the impersonation binding names it, and GCP rejects a binding whose `group:` member does not resolve.

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
[§3](#3-values-that-only-exist-after-the-first-up). Every other key the program reads must already hold its real value:
`preview` stops at the first `config.require*` the stack does not satisfy, before anything is created.

```sh
cd infra
pulumi login gs://cpg-themis-dev-pulumi-state
# Pass the secrets provider explicitly at init — `stack init` does not read it
# from the committed Pulumi.dev.yaml; without it Pulumi falls back to passphrase.
pulumi stack init dev \
  --secrets-provider="gcpkms://projects/cpg-themis-dev/locations/australia-southeast1/keyRings/themis/cryptoKeys/pulumi"
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

### Retiring a first-deploy image placeholder

`preview` reads each Cloud Run service's live image so a plan shows no spurious image change, which needs the service to
exist. A service added in the same change has none, so `preview.yml` passes a placeholder for it. Once the first deploy
creates the service, delete that line: the override keeps winning otherwise, and every later preview plans the real
image back to the placeholder.

### Adopting a service account created ahead of the program

The reverse of the placeholders above: a service account whose identity a third party has to pin *before* the program
runs. Registering an Anthropic federation rule needs the GCP SA's `email` and never-reissued `unique_id`, and that
registration is an admin roundtrip outside this repo — so the SA is created by hand first and the roundtrip starts while
the Pulumi change is still in review.

Pulumi does not adopt an existing resource. Left alone, the first `up` tries to create the account, gets
`409 AlreadyExists`, and aborts the whole stack update. Import it in the same step, matching the declaration's component
parent so the URN agrees — a top-level import creates a *different* URN, which Pulumi later reads as delete-plus-create,
issuing a fresh `unique_id` and stranding the rule that pinned the old one:

```sh
gcloud iam service-accounts create themis-convert-worker --project=<project> \
  --display-name='Themis full-text convert worker runtime'
gcloud iam service-accounts describe themis-convert-worker@<project>.iam.gserviceaccount.com \
  --format='value[separator="\n"](email,uniqueId)'   # the two claims the rule pins

# …register the Anthropic svac + rule against those two values, then, once the program declares the SA:
pulumi import gcp:serviceaccount/account:Account themis-convert-worker-runtime \
  projects/<project>/serviceAccounts/themis-convert-worker@<project>.iam.gserviceaccount.com \
  --parent 'convert_worker=urn:pulumi:<stack>::themis::themis:infra:ConvertWorker::themis'
```

Import only once the declaration exists. Importing earlier leaves state holding a resource the program does not declare,
and the next `up` — including a PR preview — tries to delete it; `pulumi import` protects by default, so that update
fails instead, blocking every deploy on the stack until the declaration lands.

The SA carries `protect` and `retain_on_delete` for the same reason the import has to be parent-matched: a replacement
reissues the `unique_id` and silently breaks token exchange.

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
