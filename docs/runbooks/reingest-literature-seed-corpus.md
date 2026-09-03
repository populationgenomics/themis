# Runbook: re-ingest the literature seed corpus

Run the litcache seed-ingestion pipeline on Dataflow against the full-text store — to complete a pass that left papers
behind, or to rebuild the corpus after a stored artifact changed shape. The corpus and its layout:
[`literature-cache.md`](../plans/literature-cache.md) (the seed is `gs://<project>-fulltext/ingest/`, ~38k papers; the
cache is `papers/` beside it). The pipeline: [`ingest_beam.py`](../../themis/litcache/ingest_beam.py). The launcher,
whose docstring is the flag reference: [`launch_dataflow.py`](../../tools/litcache/launch_dataflow.py).

Every command below reads these; set them once.

```sh
PROJECT=cpg-themis-dev
REGION=australia-southeast1
FULLTEXT=$PROJECT-fulltext
SCRATCH=$PROJECT-litcache-scratch
INGEST_SA=themis-ingest@$PROJECT.iam.gserviceaccount.com
IMAGE=$REGION-docker.pkg.dev/$PROJECT/themis/litcache-worker:$(git rev-parse --short HEAD)
ME=$(gcloud config get-value account)
```

## What a re-run does to the existing cache

A run never re-converts or replaces a committed paper (the one field it may touch is `equivalence`, when a new paper's
ids bridge two cached ones). Per paper, the write half claims the paper's external ids in the shared crosswalk and then
probes for its manifest ([`pipeline.ingest_paper`](../../themis/litcache/pipeline.py)):

- **Crosswalk** — `crosswalk.mint` adopts the incumbent `doc_id` when any of the paper's ids is already claimed, and
  mints a fresh uuid4 only when none is ([`crosswalk.py`](../../themis/litcache/crosswalk.py)). A re-run of a cached
  paper adopts; it counts as `doc_id_adopted`.
- **Manifest** — `papers/{doc_id}/manifest.pb` present ⇒ the paper is returned as-is, before any fetch or conversion; it
  counts as `paper_skipped`. No overwrite, no new source revision, no new rendering. `writer.write_paper` repeats the
  check and commits create-only (`if_generation_match=0`), so even a race cannot replace a manifest
  ([`writer.py`](../../themis/litcache/writer.py)).

So a plain re-run over the whole seed is idempotent and does one thing: it completes the papers that have no manifest —
those dead-lettered last time (unresolved metadata, a write-half exception, an oversized pdf) and those a crash left
claimed but uncommitted. Each is re-done under the `doc_id` its crosswalk row already holds. Nothing else changes —
though a skip is not free: the write stage downloads a paper's seed json and pdf before it learns the manifest exists
([`ingest_beam._WritePaperFn`](../../themis/litcache/ingest_beam.py)), so a full re-run still moves the whole seed.

There is no flag that forces reprocessing. To redo a committed paper, remove its directory and re-run:

```sh
gcloud storage rm -r "gs://$FULLTEXT/papers/$DOC_ID"
```

The run then adopts the same `doc_id` from the crosswalk and rewrites the paper under it. Two things go with the
directory: any rendering the on-demand producer added after ingestion ([`produce.py`](../../themis/litcache/produce.py))
and the `.fetch_outcome` marker ([`outcome.py`](../../themis/litcache/outcome.py)) — both are regenerated on demand. The
bucket is versioned with a 30-day noncurrent window ([`infra/README.md` §Storage](../../infra/README.md#storage)), so
the deletion is recoverable for that long.

Delete the whole directory, not a file in it — with one exception. The skip keys on `manifest.pb` alone, so a paper
missing a rendering blob or a source is skipped for good and nothing regenerates it; the ingestion run has no notion of
repairing a committed paper's artifacts, and `rebuild.py` rebuilds the crosswalk table, not the bucket. The exception is
`metadata.pb`: it derives from the paper's identifiers, not from anything else in the directory, so it can be
regenerated without re-converting. To refresh the bibliographic records and keep every conversion — the path for a
change to the record's shape, such as the metadata envelope — delete the `metadata.pb` objects and run
[`refresh_metadata.py`](../../tools/litcache/refresh_metadata.py), which resolves every committed paper without one
through the same batched ladder ingestion uses, in this process, with no Dataflow:

```sh
gcloud storage rm "gs://$FULLTEXT/papers/*/metadata.pb"
uv run --group litcache python -m tools.litcache.refresh_metadata --project="$PROJECT" --dry-run
uv run --group litcache python -m tools.litcache.refresh_metadata --project="$PROJECT"
```

It never touches a `metadata.pb` that exists, so it is safe to re-run until it exits 0; a non-zero exit lists each paper
it could not refresh and why (an unreadable manifest, no pmid or doi to resolve on, a resolver miss). Until it
completes, the evidence service's `describe_paper` titles an affected paper by its DOI or PMID
([`literature/litcache.py`](../../themis/services/evidence/literature/litcache.py)) — the only reader of the record.

A **full rebuild** is the same operation over the whole prefix. It is what a destructive change to a stored artifact
needs — the condition [`migrations.md` §How it runs](../design/migrations.md#how-it-runs) puts on such a change — and
the choice is whether to keep the crosswalk:

- **Keep it** — clear `papers/` only. Every paper is rewritten under its existing `doc_id`, so anything holding a
  `doc_id` still resolves afterwards. Until the pass completes, the evidence service's lookup
  ([`literature-evidence-layer.md`](../design/literature-evidence-layer.md)) resolves ids to `doc_id`s whose manifest is
  gone, and a paper whose ids bridge two incumbents dead-letters as an "orphan incumbent" until the incumbents are
  rewritten (a second pass heals it).
- **Clear it too** — `TRUNCATE litcache.crosswalk`, then clear `papers/`. Every paper is minted afresh; until the pass
  completes, lookups miss cleanly (the paper reads as absent, not broken). Clear the table before the prefix, so no
  window has rows pointing at nothing.

```sh
uv run python -m tools.psql -- -c 'TRUNCATE litcache.crosswalk'   # themis-clu inherits the migrator's rights
gcloud storage rm -r "gs://$FULLTEXT/papers"
```

Either way the seed under `ingest/` must still exist — it is the only input. The plan marks that prefix transient, to be
deleted once ingestion succeeds; every path in this runbook is closed the day that happens, so decide that before
running `teardown_seed` (below).

## Prerequisites

Locally: `uv sync --group dataflow`, `gcloud auth application-default login` (the launcher stages and submits under your
ADC), Docker able to build `linux/amd64`.

### The worker service account (provisioned in Pulumi)

`themis-ingest` and its standing grants are declared in [`ingest.py`](../../infra/themis_infra/ingest.py) and
[`ingest_network.py`](../../infra/themis_infra/ingest_network.py): `dataflow.worker`, `networkUser` on the
`themis-ingest` subnet (also for the Dataflow service agent), `storage.objectUser` on the full-text bucket, the Cloud
SQL connect roles and IAM DB login; the table rights (`SELECT, INSERT` on `litcache.crosswalk`) are migration
[`0007`](../../themis/migrate/migrations/0007_litcache_crosswalk_grant.sql), applied at deploy. Nothing to do here
unless a deploy has not run since those landed.

### Granted out of band

Nothing below is in Pulumi. The launcher docstring calls these "granted out of band for a bounded run"; check each
before the first run in an environment (someone with project IAM rights applies them):

```sh
# The scratch bucket itself — staging/temp for every run shape, the sample's store for a staged one.
gcloud storage buckets create "gs://$SCRATCH" --project="$PROJECT" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention

# Worker SA: pull the image, read/write the scratch bucket.
gcloud artifacts repositories add-iam-policy-binding themis --project="$PROJECT" --location="$REGION" \
  --member="serviceAccount:$INGEST_SA" --role=roles/artifactregistry.reader
gcloud storage buckets add-iam-policy-binding "gs://$SCRATCH" \
  --member="serviceAccount:$INGEST_SA" --role=roles/storage.objectUser

# You: submit jobs, run them as the worker SA, push the image, stage and read the buckets.
gcloud projects add-iam-policy-binding "$PROJECT" --member="user:$ME" --role=roles/dataflow.developer
gcloud iam service-accounts add-iam-policy-binding "$INGEST_SA" --project="$PROJECT" \
  --member="user:$ME" --role=roles/iam.serviceAccountUser
gcloud artifacts repositories add-iam-policy-binding themis --project="$PROJECT" --location="$REGION" \
  --member="user:$ME" --role=roles/artifactregistry.writer
gcloud storage buckets add-iam-policy-binding "gs://$SCRATCH" --member="user:$ME" --role=roles/storage.objectAdmin
gcloud storage buckets add-iam-policy-binding "gs://$FULLTEXT" --member="user:$ME" --role=roles/storage.objectAdmin
```

The last one is wider than reading: on a direct run the launcher itself lists `ingest/`, scans every manifest for the
report, and writes the dead-letter summary into the store; teardown deletes from it. For the crosswalk step of a full
rebuild you also need the `themis-clu` group ([`hand-driving-a-service.md`](hand-driving-a-service.md)).

## 1. Build and push the worker image

The image is not in `.github/images.json`, so CI neither builds nor pushes it — it reaches the job through
`--sdk-image`, built by hand from the repo root ([`tools/litcache/Dockerfile`](../../tools/litcache/Dockerfile)):

```sh
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
docker build --platform linux/amd64 -f tools/litcache/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"
```

The build fails, rather than the worker, if the pipeline's imports are unsatisfiable. One thing it cannot check: the
Beam SDK the Dockerfile grafts its bootloader from must be the version the launcher submits with. Compare the
`FROM apache/beam_python3.13_sdk:<version>` line against
`uv run --group dataflow python -c 'import apache_beam; print(apache_beam.__version__)'` — a mismatch surfaces only as
workers failing to start.

Tag with the commit, as here, so a job's image says which pipeline code it ran.

## 2. Rehearse: a staged sample

The default run shape copies the first `--limit` seed pairs (in stem order — the same papers every time) into the
scratch bucket and ingests from there, so `papers/` lands in the scratch bucket and the real store is untouched:

```sh
uv run --group dataflow python -m tools.litcache.launch_dataflow \
  --project="$PROJECT" --region="$REGION" --sdk-image="$IMAGE" --limit 50
```

It exercises everything the real run does — the image, the subnet, the SA, the Cloud SQL mint, live NCBI and OA egress —
and stays cheap. Read the report it prints at the end: `counters` is the per-stage tally for this run (`papers_seen`
should equal `paper_written + paper_skipped + paper_unresolved + paper_failed`); `dead-lettered` is the count of records
under `diagnostics/dead_letters/<run>/`, consolidated into `<run>.jsonl` beside it with a `reason` per paper;
`no_text_layer` lists pdf-derived papers with no recoverable character layer, a data-quality signal rather than a
failure. A second staged run reports every paper it wrote the first time as `paper_skipped` — that is the idempotency
check.

The isolation is storage only. The crosswalk is the shared one, so a staged run mints rows whose manifests live in the
scratch bucket. That is harmless to the direct run — it adopts the row and, finding no manifest in the store, writes the
paper — but until then the evidence service resolves those ids to `doc_id`s the store cannot serve.

A rehearsal with no Dataflow at all is [`run_local.py`](../../tools/litcache/run_local.py): the same pipeline on the
DirectRunner over the scratch bucket and a throwaway Postgres, for checking pipeline code before an image is built.

## 3. The real run

`--direct` reads `ingest/` from and writes `papers/` to the store, with no staging. `--limit` is then optional and still
honoured, so a bounded direct run is a canary against the real store before the full pass:

```sh
uv run --group dataflow python -m tools.litcache.launch_dataflow \
  --project="$PROJECT" --region="$REGION" --sdk-image="$IMAGE" --direct --limit 200
```

Then the whole corpus. Two defaults are sized for a sample: `--max-workers` (2) caps the autoscaled write stage, and
`--max-dead-letters` (0) makes any dead letter an exit 1. Over ~38k papers some dead letters are expected — the corpus
has a handful of 200–450 MB pdfs the identity stage refuses by size, and some ids will not resolve — so set a tolerance
you are prepared to review rather than treat the exit code as the verdict:

```sh
uv run --group dataflow python -m tools.litcache.launch_dataflow \
  --project="$PROJECT" --region="$REGION" --sdk-image="$IMAGE" --direct \
  --max-workers 8 --max-dead-letters 200
```

Two things bound `--max-workers`. The write stage's OA fetches leave every worker in parallel, so the upstream sources
see the fan-out. And each worker process holds one crosswalk connection for its lifetime
([`ingest_beam._MintConnection`](../../themis/litcache/ingest_beam.py)) — two per `n1-standard-2` — against a shared
instance on the smallest tier ([`sql.py`](../../infra/themis_infra/sql.py); `db-f1-micro` allows 25) that the services
also connect to. The resolution stage is unaffected by the flag (below).

### Watching it

The launcher blocks on the job and names it `litcache-ingest-<UTC timestamp>` (the job carries a
`cost-monitor-expiry=48h` label). From another terminal:

```sh
gcloud dataflow jobs list --project="$PROJECT" --region="$REGION" --status=active
gcloud dataflow jobs describe "$JOB_ID" --project="$PROJECT" --region="$REGION"
```

or the console at `https://console.cloud.google.com/dataflow/jobs/$REGION/$JOB_ID?project=$PROJECT`, whose Job metrics
panel shows the `litcache.ingest` counters live. The graph has three stages, and their shapes differ:

- `ExtractIdentity` and `WritePaper` are per paper, isolated, and scale with the worker pool.
- `ResolveMetadata` runs on **one worker** by design: every paper's ids are grouped onto a single key so the whole
  seed's bibliographic resolution goes out as ~⌈N/200⌉ batched NCBI calls from one place, bounding the request rate
  independent of worker count. The pool idles down to it and back up for the write stage. It is also the run's one
  unisolated step — the seed's metadata is resident on that `n1-standard-2`, and a transport failure that outlives the
  batch's retries fails the job rather than a paper.

No measured throughput for the full corpus is recorded in the repo. Time the canary's write stage in the console and
scale it by paper count; the resolution stage scales with the number of batched calls (~⌈N/200⌉) and NCBI's response
time, not with workers.

If the launcher process dies, the job continues, but the report and the consolidated dead-letter summary are never
written (they need the launcher's handle on the job's metrics). The per-paper records under
`diagnostics/dead_letters/<run>/` are still there, and the console still has the counters.

### Telling it succeeded

The launcher prints the report once the job reaches `DONE`, then
`Output written to gs://<bucket>/papers/; crosswalk minted into <instance>`. Success is the counters and the dead-letter
list, not the exit code:

- `papers_seen` is one per paired seed object (the launcher logs unpaired objects — a `.json` with no `.pdf` or the
  reverse — as excluded; they are never ingested).
- `paper_written + paper_skipped` is what the store now holds from this seed; every remaining paper is a line in
  `gs://$FULLTEXT/diagnostics/dead_letters/<run>.jsonl` with its reason.
- The `papers written:` line at the top of the report is the count of every manifest under `papers/`, not this run's
  writes; on a direct run that is the whole store. Producing it downloads each manifest from your machine, which takes a
  while after the job itself has finished — leave the launcher running.

Dead letters are re-tried by re-running: they have no manifest, so a second `--direct` pass picks up exactly them and
skips the rest. Fix what the reasons point at first (a transient upstream is worth a plain retry; an oversized pdf is
not).

## 4. Cleanup

Everything a staged run leaves is in the scratch bucket: the copied `ingest/`, the sample `papers/`, Dataflow's
`dataflow/staging` and `dataflow/tmp`, and `diagnostics/`. A direct run leaves only `dataflow/` there. Delete what you
no longer need; the crosswalk rows staged runs minted stay, and are healed or superseded by the next direct run.

```sh
gcloud storage rm -r "gs://$SCRATCH/dataflow" "gs://$SCRATCH/ingest" "gs://$SCRATCH/papers" "gs://$SCRATCH/diagnostics"
```

`report.teardown_seed` deletes the store's `ingest/` ([`report.py`](../../themis/litcache/report.py)) — a manual step by
design, taken only after the report confirms every paper is committed, and one that forecloses every re-ingestion this
runbook describes. Do not take it as part of a routine re-run.

## What can go wrong

The launcher's own exits:

- `--limit is required unless --direct` (or a non-positive `--limit`, a negative `--max-dead-letters`) — argument
  errors, before anything is touched.
- `Dataflow job did not complete cleanly (state: …)` — the job failed or was cancelled. Read the job's logs in the
  console; the per-paper failures below never cause this, since they are dead-lettered.
- Exit 1 with `N paper(s) dead-lettered, over the --max-dead-letters tolerance` — the job finished, the store is
  updated, and the summary path in the message lists what was not. This is a review prompt, not a failed run.

Permission failures, and which grant each points at:

| Symptom                                                                                | Missing                                                            |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `403` while staging (`copy_blob`) or listing `ingest/`                                 | You: read on `$FULLTEXT`, write on `$SCRATCH`                      |
| `dataflow.jobs.create` denied at submit                                                | You: `roles/dataflow.developer`                                    |
| `Current user cannot act as service account themis-ingest@…`                           | You: `iam.serviceAccountUser` on `$INGEST_SA`                      |
| Workers never start; job log names the image or `artifactregistry`                     | `$INGEST_SA`: `artifactregistry.reader` on `themis`                |
| Workers never start; job log names the subnetwork or `compute.subnetworks.use`         | Pulumi (`ingest.py` `networkUser`) — has the stack deployed?       |
| Workers fail on `dataflow/tmp` or `dataflow/staging`                                   | `$INGEST_SA`: `objectUser` on `$SCRATCH`                           |
| `permission denied for schema litcache` / `relation litcache.crosswalk` in worker logs | Migration `0007` not applied — run a deploy                        |
| Cloud SQL connect refused for `themis-ingest@…iam`                                     | Pulumi (`ingest.py` connect roles + IAM DB user)                   |
| Report step `403` writing `diagnostics/dead_letters/<run>.jsonl` on a direct run       | You: write on `$FULLTEXT`                                          |
| SDK harness version mismatch at worker start                                           | Dockerfile's `beam_python3.13_sdk` tag vs the locked `apache-beam` |

A job that fails in `ResolveMetadata` (NCBI unreachable past the retry budget, or the single resolve worker running out
of memory) has written nothing to `papers/` — the identity stage's results are discarded with the job, and a re-run
starts over; only its dead-letter records (unreadable or oversized seeds) are already on the bucket. A bigger machine
for that stage is a launcher change (`--worker_machine_type`), not a flag.
