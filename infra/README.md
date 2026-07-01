# infra

Cloud infrastructure for Themis: a Pulumi program (Python), **one stack per environment** (`dev` now, `prod` later),
each its own GCP project. Cloud-only — no application code (that's `apps/`). The same program runs every environment;
all differences live in `Pulumi.<stack>.yaml`.

## Layout

| Path                         | What                                                                                                        |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `Pulumi.yaml`                | Project + Python runtime (uses the repo venv `../.venv`). No `backend:` — state is per-environment (below). |
| `Pulumi.<stack>.yaml`        | Per-environment config + `gcpkms` secrets provider.                                                         |
| `__main__.py`                | Entrypoint: read config, compose the modules, export outputs.                                               |
| `themis_infra/baseline.py`   | Enabled GCP services + the shared Artifact Registry.                                                        |
| `themis_infra/web.py`        | Cloud Run web app + external HTTPS LB + IAP; its runtime SA is the Managed-Agents client identity.          |
| `themis_infra/storage.py`    | The literature full-text store bucket (durable GCS).                                                        |
| `themis_infra/secrets.py`    | Ingestion API-key secrets (Secret Manager) sourced from encrypted config.                                   |
| `themis_infra/ingest.py`     | The litcache ingestion runtime SA (Dataflow worker) + its data-plane grants.                                |
| `themis_infra/deploy_iam.py` | The CI deploy SA's build-time project roles (bootstrap keeps only the IAM/state root).                      |
| `bootstrap/bootstrap.sh`     | One-time substrate setup (below). Run locally, never CI.                                                    |

Database and audit arrive as sibling modules under `themis_infra/`, composed in `__main__.py` — still one `pulumi up`.

## Storage

The literature **full-text store** (per-paper PDFs/XML, derived markdown, figures, knowledge units —
`docs/design/literature-evidence-layer.md` §2.1) lives in a per-environment GCS bucket,
`gs://cpg-themis-<env>-fulltext`. It is the durable source of truth; Cloud SQL is a rebuildable projection of it. Named
for its content (full text): it never expires live objects (so not the design doc's "cache"), and stays distinct from
the 37M abstract *corpus* (in Cloud SQL, not a bucket). Policy:

- **Private** — uniform bucket-level access + enforced public-access prevention (it holds copyrighted source PDFs).
- **Versioned, 30-day window; soft delete off** — recovery is object versioning: a superseded (noncurrent) version is
  kept 30 days for accidental delete/overwrite recovery, then GC'd by a lifecycle rule. Soft delete (GCS's default 7-day
  guard) is explicitly disabled, because its window can't be overridden — already-soft-deleted objects ride out the full
  window regardless of policy, trapping a *deliberate* reclaim — whereas versioning lets an intentional
  `gcloud storage rm --all-versions` reclaim immediately. Live content is never auto-expired; this bounds only the
  version history.
- **Autoclass (terminal Archive)** — GCS moves cold objects toward Archive and back to Standard on read, with no
  retrieval/early-deletion fees; the store is large and read-rarely after ingestion, so this minimises idle storage
  cost.

Deletion is a safeguard, not a lock: `force_destroy` is False so `pulumi destroy` won't drop a non-empty bucket, but
intentional removal — a copyright takedown, a retraction — is always available manually (`gcloud storage rm`, or
empty-then-destroy).

A dedicated bucket per storage concern (not one shared bucket): these are bucket-level policies that can't be
prefix-scoped, and the parquet/audit consumers the design anticipates need different whole-bucket profiles. The
ingestion runtime's read/write grant is in `themis_infra/ingest.py`; the reader grant is still deferred. In dev,
operators use their own IAM-gated `gcloud` ADC.

## Two tiers: bootstrap vs program

- **`bootstrap.sh`** creates only what Pulumi itself needs to already exist: the per-environment state bucket, the KMS
  key for the secrets provider, the GitHub WIF pool + the deploy/preview service accounts, and baseline network
  hardening (drops the default VPC and its permissive firewall rules). Run once per environment by an operator with
  Owner. Idempotent.
- **The Pulumi program** is everything else, in one `pulumi up`.

## State and secrets

- **State is isolated per environment** — `gs://cpg-themis-<env>-pulumi-state`, each in its own project. `Pulumi.yaml`
  has no `backend:` (it's shared across stacks); the backend is selected per environment instead: CI passes
  `--cloud-url`, locally `pulumi login gs://cpg-themis-dev-pulumi-state`.
- **Secrets** use the `gcpkms` provider (per-stack KMS key): the value goes in encrypted stack config
  (`pulumi config set --secret themis:<key>`), the program reads it with `config.require_secret(...)`, and — for a
  runtime credential — provisions it into Secret Manager (`themis_infra/secrets.py`) so the workload reads it there, not
  from Pulumi config. First one landed: `themis:semanticScholarApiKey` → the `semantic-scholar-api-key` secret.

## Config

Per-environment (in `Pulumi.<stack>.yaml`): `gcp:project`, `gcp:region`, `themis:domain`, `themis:iapAccessGroup`. The
deployed image is a per-run input, not committed config: set `THEMIS_WEB_IMAGE` (env var) to the image to deploy —
required, no default (fail loud).

## Lifecycle (a fresh environment)

See [`docs/runbooks/fresh-environment.md`](../docs/runbooks/fresh-environment.md) for the full runbook. In short:

1. `PROJECT=cpg-themis-dev infra/bootstrap/bootstrap.sh`
1. First bring-up (creates the registry + edge running a placeholder):
   ```sh
   cd infra && pulumi login gs://cpg-themis-dev-pulumi-state && pulumi stack init dev
   THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi preview   # review, then `up`
   pulumi stack output lb_ip                                # hand to IT for the A record
   ```
1. Thereafter CI owns deploys: PRs get a read-only `pulumi preview` comment (`preview.yml`); merge to `main` builds the
   image and runs `pulumi up` (`deploy.yml`).

## Adding an environment

Add `Pulumi.prod.yaml` (its project, hostname, access group, KMS key) and run
`PROJECT=cpg-themis-prod infra/bootstrap/bootstrap.sh`. No program change.

## Local development

`uv sync --group infra` populates `../.venv` (the venv Pulumi runs the program in). Then
`pulumi login gs://cpg-themis-<env>-pulumi-state` and preview. The deployed image is a required input (no default — fail
loud), so pin it to the live image rather than the placeholder, mirroring `preview.yml`:

```sh
THEMIS_WEB_IMAGE=$(gcloud run services describe themis-web \
  --project=cpg-themis-dev --region=australia-southeast1 \
  --format='value(spec.template.spec.containers[0].image)') \
  pulumi preview
```

Local operations use your own `gcloud` ADC (`gcloud auth application-default login`), IAM-gated.
