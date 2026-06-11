# Design: Deployment — IaC, deploy auth, state, and secrets

**Parent epic:** [`issues/epic-themis-spike.md`](../../issues/epic-themis-spike.md) (PR #1)
**Related:** [`spike-infrastructure.md`](spike-infrastructure.md) —
what infrastructure the Spike needs; this doc decides how it is
managed. Resolves that exploration's IaC-tool and secrets-management
questions and the auth part of its CI/CD question.

Constraint: every tracked file is mirrored publicly. The only hard
exclusion is plaintext secret material. Identifiers and KMS-gated
ciphertext are fine ("What lives where"); the leak-screen and
security review policies are refined in step with this doc.

## Decisions

| Concern | Decision |
| --- | --- |
| IaC tool | Pulumi (Python SDK), program tracked in this repo |
| Deploy auth, CI | GitHub Actions OIDC → Workload Identity Federation; no service-account keys |
| Deploy auth, local | Personal `gcloud` ADC, IAM-gated |
| State backend | Versioned, private GCS bucket (Pulumi DIY backend) |
| Secrets encryption | `gcpkms` secrets provider (Cloud KMS key, IAM-gated) |
| Secret values | Runtime fetch from Secret Manager; Pulumi provisions containers and IAM bindings, not versions |
| Repo contents | Standard Pulumi layout incl. `Pulumi.<stack>.yaml`; no plaintext secrets, ever |

## Deploy authentication

CI: the workflow exchanges a short-lived GitHub OIDC token via WIF
to impersonate a deploy service account. No long-lived credential
exists anywhere. The WIF provider name and deploy-SA email sit in
the workflow file; GitHub holds no secrets.

- The WIF attribute condition pins `attribute.repository ==
  "populationgenomics/themis-internal"` and the deploying ref. OIDC
  tokens name the requesting repository, so the public mirror (a
  different repo, Actions disabled) cannot satisfy it.
- Deploy jobs and `pulumi preview` against real state run only on
  `push` to `main` / a protected environment, never on
  `pull_request`. PR jobs (including agentic review) get no
  `id-token: write` and no cloud access. Cloud-free validation
  (lint, type-check, unit tests) runs on PRs.

Local: `gcloud auth application-default login`; IAM grants to named
individuals.

## State

Dedicated private GCS bucket (`pulumi login gs://…`): uniform
bucket-level access, object versioning, IAM limited to the deploy SA
and developers. State may contain secret ciphertext; bucket privacy
and the secrets provider are independent layers. `backend.url` is
committed in `Pulumi.yaml` — a bucket name is an identifier, not a
credential, and this removes per-machine backend setup.

## Secrets encryption

`gcpkms`, not `passphrase`:

```
pulumi stack init prod \
  --secrets-provider="gcpkms://projects/…/locations/…/keyRings/…/cryptoKeys/…"
```

- Decryption authority is IAM
  (`roles/cloudkms.cryptoKeyEncrypterDecrypter`) on the key — the
  same identity plane as deploys. No shared passphrase to
  distribute, rotate, or revoke.
- Every decrypt lands in Cloud Audit Logs.
- The wrapped data key is inert without KMS access; no offline
  brute-force path. Passphrase ciphertext is offline-bruteforceable.

## Secret values

App secrets (Anthropic API key, DB credentials, third-party tokens)
live in Secret Manager, read at runtime via the workload SA
(`roles/secretmanager.secretAccessor`). Pulumi provisions the
`Secret` containers and IAM bindings, not the versions; versions are
populated out of band. Rotation is a Secret Manager operation,
invisible to IaC.

Exception: values Pulumi needs at provision time (e.g. a generated
Cloud SQL password via `RandomPassword`) are secret-tracked in state
as KMS ciphertext.

## What lives where

| Artifact | Lives |
| --- | --- |
| Pulumi program, `Pulumi.yaml`, `Pulumi.<stack>.yaml` | this repo; secret values only as KMS ciphertext |
| State checkpoints | state bucket |
| Runtime secrets | Secret Manager, populated out of band |
| KMS key | Cloud KMS, IAM-gated, audit-logged |

Committing `Pulumi.<stack>.yaml` (secrets-provider URL, project ID,
region, resource names, ciphertext) is safe on the public mirror:
identifiers are not credentials (access is IAM), ciphertext is
KMS-gated, and the mirror carries only `main`'s files — CI, PR
discussion, and logs stay in this private repo.

Never in the repo or logs: plaintext secret values, service-account
keys (none exist), identifiers of CPG infrastructure beyond the
Spike's deploy project. The Spike is isolated and
public-endpoints-only, so its config discloses nothing about the
data estate.

Bootstrap: the state bucket, KMS key ring, and WIF pool predate the
first `pulumi up`; created once by a documented `gcloud` script
(spike-infrastructure runbook deliverable).

## Why Pulumi

Candidates: Pulumi, Terraform/OpenTofu, `gcloud` scripts, manual +
runbook. Manual fails reproducibility; scripts aren't idempotent or
drift-aware. Versus Terraform:

- Python program — same language and CI toolchain (ruff, pyright,
  pytest) as the rest of the repo; HCL is a second language.
- Secrets are encrypted inside state; Terraform state is plaintext,
  protected only at the storage layer (OpenTofu has client-side
  state encryption, younger ecosystem).
- Licensing: Terraform is BUSL since 2023; Pulumi CLI/SDKs are
  Apache-2.0.

Accepted trade-offs: smaller module ecosystem and community.
Pulumi's GCP provider is bridged from Terraform's, so resource
coverage is equivalent. Pulumi Cloud / ESC is the upgrade path if
self-managing chafes; the DIY backend keeps the Spike self-hosted.

## Out of scope — stays with spike-infrastructure

- GCP project layout, IAP/SSO, cost observability, audit/retention,
  agent sandbox image.
- CI/CD pipeline shape beyond auth: Artifact Registry, image build,
  promotion path, preview environments.
- Per-secret inventory and rotation cadence.
- The fresh-environment runbook (absorbs the bootstrap steps).
