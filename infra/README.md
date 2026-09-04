# infra

Cloud infrastructure for Themis: a Pulumi program (Python), **one stack per environment** (`dev` now, `prod` later),
each its own GCP project. Cloud-only — no application code (that's `apps/`). The same program runs every environment;
all differences live in `Pulumi.<stack>.yaml`.

## Layout

| Path                          | What                                                                                                                                                                                      |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Pulumi.yaml`                 | Project + Python runtime (uses the repo venv `../.venv`). No `backend:` — state is per-environment (below).                                                                               |
| `Pulumi.<stack>.yaml`         | Per-environment config + `gcpkms` secrets provider.                                                                                                                                       |
| `__main__.py`                 | Entrypoint: read config, compose the modules, export outputs.                                                                                                                             |
| `themis_infra/baseline.py`    | Enabled GCP services + the shared Artifact Registry.                                                                                                                                      |
| `themis_infra/web.py`         | Cloud Run web app + external HTTPS LB + IAP; its runtime SA is the Managed-Agents client identity.                                                                                        |
| `themis_infra/auth.py`        | The auth data-plane gRPC service (internal-ingress Cloud Run) + its runtime SA and Cloud SQL IAM DB login.                                                                                |
| `themis_infra/store.py`       | The store data-plane gRPC service (internal-ingress Cloud Run) + its runtime SA and working-document/workspace GCS buckets.                                                               |
| `themis_infra/sql.py`         | Cloud SQL (Postgres) instance, IAM database auth, backups + PITR; the app data store.                                                                                                     |
| `themis_infra/storage.py`     | The durable GCS buckets shared across the data plane: the literature full-text store, the resources bucket.                                                                               |
| `themis_infra/convert.py`     | The on-demand full-text conversion lane: the Cloud Tasks queue, the pushed convert worker (Cloud Run), and the task invoker identity.                                                     |
| `themis_infra/cost.py`        | The workspace-spend monitor: the cost exporter's runtime SA, the GCP side of its Anthropic WIF identity.                                                                                  |
| `themis_infra/screenshots.py` | The public-read PR review screenshot bucket (get-without-list, so it is not enumerable).                                                                                                  |
| `themis_infra/secrets.py`     | Ingestion API-key secrets (Secret Manager) sourced from encrypted config.                                                                                                                 |
| `themis_infra/ingest.py`      | The litcache ingestion runtime SA (Dataflow worker) + its data-plane grants. Running a pass: [`reingest-literature-seed-corpus.md`](../docs/runbooks/reingest-literature-seed-corpus.md). |
| `themis_infra/sandbox.py`     | Self-hosted sandbox substrate: dedicated VPC/subnet, deny-all egress firewall, DNS sinkhole policy, session-token KMS key, and the Anthropic environment-key secret.                      |
| `themis_infra/clu.py`         | The impersonated identity a person calls a backend service as, and the group permitted to impersonate it.                                                                                 |
| `themis_infra/deploy_iam.py`  | The CI deploy SA's build-time project roles (bootstrap keeps only the IAM/state root).                                                                                                    |
| `bootstrap/bootstrap.sh`      | One-time substrate setup (below). Run locally, never CI.                                                                                                                                  |

Audit arrives as a sibling module under `themis_infra/`, composed in `__main__.py` — still one `pulumi up`.

## Storage

The literature **full-text store** (per-paper PDFs/XML, derived markdown, figures, knowledge units —
`docs/design/literature-evidence-layer.md`) lives in a per-environment GCS bucket, `gs://cpg-themis-<env>-fulltext`. It
is the durable source of truth; Cloud SQL is a rebuildable projection of it. Named for its content (full text): it never
expires live objects (so not the design doc's "cache"), and stays distinct from the 37M abstract *corpus* (in Cloud SQL,
not a bucket). Policy:

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

The store's **workspace** bucket, `gs://cpg-themis-<env>-store-workspace`, carries the same policy — versioned with the
same 30-day noncurrent window, soft delete off, Autoclass to Archive — and no age-based delete rule. The constraint is
the sheaf repository it is shaped to hold ([`docs/design/sheaf.md`](../docs/design/sheaf.md)): packfiles are written
once and never rewritten, so a rule that deletes by age would remove a long-lived repository's base pack while the ref
document still names it, whereas a noncurrent-age rule cannot touch a pack, since a pack never has a noncurrent
generation. Nothing in that design deletes, so what accumulates sinks to Archive rather than being reclaimed. For the
single tar archive a live session rewrites each turn, each rewrite makes the previous generation noncurrent and those
expire after 30 days; the current archive persists until something deletes it.

The **PR review screenshot** bucket, `gs://cpg-themis-dev-pr-screenshots`, is the one exception to that private policy
and the only bucket in the project that permits public access: GitHub renders a PR-body image through its Camo proxy,
which fetches the origin anonymously, so the read is necessarily unauthenticated
([`docs/design/pr-screenshots.md`](../docs/design/pr-screenshots.md)). `allUsers` holds
`roles/storage.legacyObjectReader` — `storage.objects.get` and nothing else — so an object is fetchable by exact name
but the bucket is not enumerable; the `themis-dev-access` group holds `objectAdmin` so an uploader can also retract, and
a developer's own `gcloud` ADC is the writer. Objects are `<sha256>.png`, kept forever, Autoclass-tiered, with soft
delete off so a takedown is immediate. A stack only gets a bucket if it sets `themis:enablePrScreenshotBucket` —
screenshots are of the fixture UI, so no other environment has a reason to expose a public one.

**Supplied papers** — records the open-access archive does not serve, obtained through a human's institutional access —
have no store of their own. A deposit is an ordinary litcache paper in the full-text bucket: the PDF as its source
(upload kind, institution-captured access), the standard OCR rendering as its markdown, so nothing downstream treats it
as a special case. Its recovery path is not the version history but the mirror the PDFs live in, outside any repo — the
deposit tool re-runs over it and rebuilds whatever the bucket lost
([`supplied-literature.md`](../docs/runbooks/supplied-literature.md)).

The **resources** bucket, `gs://cpg-themis-<env>-resources`, holds the reference data the Project's services and
pipelines share: public upstream mirrors and the artifacts derived from them. One dataset per top-level prefix
(`gs://<bucket>/<dataset>/...`) — `gene-disease/` for the dumps the weekly refresh job writes and the evidence service
loads at startup — each with its own provenance and its own writer, who alone holds `objectAdmin` on the bucket. It is
unversioned with soft delete at the GCS default: every object is re-derivable from a pinned upstream, so the cost of
losing one is a re-run. Autoclass tiers to Nearline rather than Archive, because a cold read is on a service cold start
or a fresh pipeline machine, where Archive retrieval fees would land on the critical path.

A bucket per storage *policy*, not per dataset: the properties above — public access, versioning, retention, storage
class — are bucket-level and can't be prefix-scoped, so datasets share a bucket exactly when they want the same answers
to all of them, and the parquet/audit consumers the design anticipates want different ones. The full-text store's
ingestion runtime holds its read/write grant in `themis_infra/ingest.py`; the readers' are in `themis_infra/evidence.py`
(the service) and `__main__.py` (the BFF, which serves the objects it 302s to). In dev, operators use their own
IAM-gated `gcloud` ADC.

## Deletion guards

Guarded only where loss is unrecoverable or externally bound: the reserved load-balancer IP (DNS points at it), the
Cloud SQL instance and its database, and the web runtime and convert-worker SAs whose never-reissued `unique_id`s their
Anthropic WIF rules pin, and the cost-exporter SA a rule will pin (`protect`, plus `retain_on_delete` on the SAs).
Buckets rely on the non-empty refusal above.

Cloud Run services and jobs set `deletion_protection=False` explicitly: the provider defaults it to true, and it is a
state-side flag rather than a GCP setting, so only a program that still declares the resource can clear it. A service
dropped from the program is therefore undeletable, and one failed delete aborts the whole update — blocking every later
deploy, not just its own.

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

Per-environment, in `Pulumi.<stack>.yaml`. The `config.require*` calls at the top of `__main__.py` are the list of what
the program reads — a missing key fails `pulumi up`; `Pulumi.dev.yaml` is the worked example of every one. Two things
that list cannot express:

- `themis:iapBackendServiceId` and `themis:anthropicFederationRuleId` name values the stack itself produces, so a fresh
  environment holds placeholders for them until its first `up`
  ([`fresh-environment.md`](../docs/runbooks/fresh-environment.md) §3).

The deployed image is a per-run input, not committed config: set `THEMIS_WEB_IMAGE` (env var) to deploy a specific image
(`deploy.yml` sets the freshly-pushed ref). With no override the program pins to the service's live image, so a preview
shows no spurious diff — except on a first bring-up, when no live service exists yet and the override is required.

## Lifecycle (a fresh environment)

[`docs/runbooks/fresh-environment.md`](../docs/runbooks/fresh-environment.md) owns the commands. The shape:

1. `bootstrap.sh`, once, by an operator with Owner.
1. First bring-up, against a placeholder image and placeholder values for the config the stack itself produces — that
   one `up` creates the registry and brings the edge up.
1. Set the values that now exist (§3) and `up` again; hand the LB IP to IT for the A record.
1. Thereafter CI owns deploys: PRs get a read-only `pulumi preview` comment (`preview.yml`); `deploy.yml` builds the
   images and runs `pulumi up` when the `deployed/<env>` branch is pushed, or when dispatched on `main`. Merging does
   not deploy — see [`docs/design/deployment.md`](../docs/design/deployment.md).

## Adding an environment

Run `PROJECT=cpg-themis-prod infra/bootstrap/bootstrap.sh` first — it creates the state bucket and the KMS key the
stack's secrets are encrypted to. Then copy `Pulumi.dev.yaml` to `Pulumi.prod.yaml` and replace every value, including
`secretsprovider`. The `secure:` entries and `encryptedkey` don't carry over: they are wrapped to dev's key, so re-set
each secret against the new stack (`pulumi config set --secret themis:<key>`). `themis:iapBackendServiceId` and
`themis:anthropicFederationRuleId` must not be copied at all — the real values only exist after the first `up`, so a
copied one breaks loudly. Two are quiet instead, deploying clean onto a wrong outcome:
`themis:enablePrScreenshotBucket`, because a copied `true` creates a world-readable bucket in an environment that has no
review workflow to justify one; and `themis:anthropicWorkerFederationRuleId`, whose rule is pinned to another
environment's service account, so the convert worker deploys healthy and fails only when it first tries to transcribe.
No program change; the full sequence is [`fresh-environment.md`](../docs/runbooks/fresh-environment.md).

## Local development

`uv sync --group infra` populates `../.venv` (the venv Pulumi runs the program in). Then
`pulumi login gs://cpg-themis-<env>-pulumi-state` and `pulumi preview`. With no `THEMIS_WEB_IMAGE` override the program
pins to the service's live image (the resolution `preview.yml` relies on), so a plain preview shows no spurious image
diff. Set `THEMIS_WEB_IMAGE` only to preview a specific image.

Local operations use your own `gcloud` ADC (`gcloud auth application-default login`), IAM-gated.
