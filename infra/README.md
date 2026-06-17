# infra

Cloud infrastructure for Themis: a Pulumi program (Python), **one stack per
environment** (`dev` now, `prod` later), each its own GCP project. Cloud-only —
no application code (that's `apps/`). The same program runs every environment;
all differences live in `Pulumi.<stack>.yaml`.

## Layout

| Path | What |
| --- | --- |
| `Pulumi.yaml` | Project + Python runtime (uses the repo venv `../.venv`). No `backend:` — state is per-environment (below). |
| `Pulumi.<stack>.yaml` | Per-environment config + `gcpkms` secrets provider. |
| `__main__.py` | Entrypoint: read config, compose the modules, export outputs. |
| `themis_infra/baseline.py` | Enabled GCP services + the shared Artifact Registry. |
| `themis_infra/web.py` | Cloud Run service + external HTTPS LB + IAP. |
| `bootstrap/bootstrap.sh` | One-time substrate setup (below). Run locally, never CI. |

Database, storage, and audit arrive as sibling modules under `themis_infra/`,
composed in `__main__.py` — still one `pulumi up`.

## Two tiers: bootstrap vs program

- **`bootstrap.sh`** creates only what Pulumi itself needs to already exist: the
  per-environment state bucket, the KMS key for the secrets provider, the GitHub
  WIF pool + the deploy/preview service accounts, and baseline network hardening
  (drops the default VPC and its permissive firewall rules). Run once per
  environment by an operator with Owner. Idempotent.
- **The Pulumi program** is everything else, in one `pulumi up`.

## State and secrets

- **State is isolated per environment** — `gs://cpg-themis-<env>-pulumi-state`,
  each in its own project. `Pulumi.yaml` has no `backend:` (it's shared across
  stacks); the backend is selected per environment instead: CI passes
  `--cloud-url`, locally `pulumi login gs://cpg-themis-dev-pulumi-state`.
- **Secrets** use the `gcpkms` provider (per-stack KMS key). The skeleton stores
  none; when one lands, use `config.require_secret(...)` / encrypted config.

## Config

Per-environment (in `Pulumi.<stack>.yaml`): `gcp:project`, `gcp:region`,
`themis:domain`, `themis:iapAccessGroup`. The deployed image is a per-run input,
not committed config: set `THEMIS_WEB_IMAGE` (env var) to the image to deploy —
required, no default (fail loud).

## Lifecycle (a fresh environment)

See [`docs/runbooks/fresh-environment.md`](../docs/runbooks/fresh-environment.md)
for the full runbook. In short:

1. `PROJECT=cpg-themis-dev infra/bootstrap/bootstrap.sh`
2. First bring-up (creates the registry + edge running a placeholder):
   ```sh
   cd infra && pulumi login gs://cpg-themis-dev-pulumi-state && pulumi stack init dev
   THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi preview   # review, then `up`
   pulumi stack output lb_ip                                # hand to IT for the A record
   ```
3. Thereafter CI owns deploys: PRs get a read-only `pulumi preview` comment
   (`preview.yml`); merge to `main` builds the image and runs `pulumi up`
   (`deploy.yml`).

## Adding an environment

Add `Pulumi.prod.yaml` (its project, hostname, access group, KMS key) and run
`PROJECT=cpg-themis-prod infra/bootstrap/bootstrap.sh`. No program change.

## Local development

`uv sync --group infra` populates `../.venv` (the venv Pulumi runs the program
in). Then `pulumi login gs://cpg-themis-<env>-pulumi-state`, `pulumi preview`.
Local operations use your own `gcloud` ADC (`gcloud auth application-default
login`), IAM-gated.
