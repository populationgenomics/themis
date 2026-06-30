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

Creates: the per-environment state bucket, the KMS key, the GitHub WIF pool + `themis-deploy` (write, main-only) /
`themis-preview` (read-only, PRs) service accounts, and network hardening (drops the default VPC + its permissive
rules). Idempotent. The deploy/preview SA emails and the WIF provider path it prints are already wired into
`.github/workflows/{deploy,preview}.yml`.

## 2. First bring-up (once per environment)

The registry is created by the program, so the first `pulumi up` uses a public placeholder image — that one `up` creates
the registry *and* brings the edge up running the placeholder; later deploys push real images to that registry.

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

## 3. DNS handoff (external — IT team)

```sh
pulumi stack output lb_ip
```

Give that IP to the IT team and ask for an **A record** `themis-dev.populationgenomics.org.au → <lb_ip>` (an A record
points a name at an IP; a CNAME can't). The Google-managed TLS certificate stays `PROVISIONING` until the record
resolves, then goes `ACTIVE` (minutes to ~an hour). The IP is `protect`ed and stable, so it's safe to hand out before
the cert is live.

## 4. Hand off to CI

Once bootstrap + the first bring-up are done, CI owns deploys: PRs get a read-only preview comment; merge to `main`
builds the image and `pulumi up`s. Nothing else manual.

## Tearing down

The reserved IP is `protect`ed; `pulumi destroy` refuses until you clear the protection (deliberate — it guards the
externally-bound address).
