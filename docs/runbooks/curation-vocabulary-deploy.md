# Runbook: moving the curation surface onto the shared vocabularies

The curation contract used to declare its own `Inheritance`, `ConsequenceClass` and `Classification`. It now names the
shared ones ([`../design/curation-surface.md`](../design/curation-surface.md) §Storage). The class ladder kept every
number, so a stored verdict is already right; the other two renumbered some of their members, so a routing row written
under the old contract may name a mode or a consequence the curator never chose. `themis.curation.vocabulary_backfill`
is the correction, and it can only run once the new revision is deployed and migration `0012_curation_vocabulary` has
taken the snapshots it reads from.

That ordering is the whole reason for a window. The deploy pipeline rolls Cloud Run before it applies migrations
([`../design/migrations.md`](../design/migrations.md) §How it runs), so from the new revision going live until this
procedure finishes, the surface's code and its rows disagree about what the numbers mean. A curator working in that gap
sees the wrong routing, and — worse — anything they save is written under the *new* numbering into rows the snapshot
then captures, where nothing can distinguish it from a row the old contract wrote. That is why `--apply` takes the time
you closed the surface and refuses to run if any snapshot row postdates it: it is the one error verification cannot see,
because it would read both sides the same wrong way.

**So step 2 comes before step 3**: the surface is closed before the new revision goes live.

## What this needs

Everything here runs as `themis-clu`, the identity [`hand-driving-a-service.md`](hand-driving-a-service.md) §The
database sets up: it inherits the migrator role, and a table's owner bypasses the grants, which is what makes
`curation.assessments` writable at all — no runtime account holds `UPDATE` on it.

The SQL steps go through `uv run python -m tools.psql`, which impersonates the account for you off your own credentials
(add `--project` outside dev, which it defaults to). Steps 2 and 6 want an interactive session rather than the `-- -c`
form: `SET ROLE` lasts only as long as the session it is issued in, and without it the table step 2 creates belongs to
`themis-clu` instead of to the migrator that owns the schema.

The rewrite is Python and dials the connector itself, so it reads ADC instead. Point ADC at the account for steps 4 and
5 only, and restore your own before step 6 — `tools.psql` impersonates *from* whatever ADC holds, and `themis-clu` has
no token-creator grant on itself, so with ADC already set to it every `tools.psql` step is refused.

```bash
gcloud auth application-default login --impersonate-service-account=themis-clu@<project>.iam.gserviceaccount.com
gcloud auth application-default login   # afterwards
```

Its instance coordinates come from `pulumi stack output` in `infra/` (with the stack selected) — `sql_connection_name`,
`sql_database`, and `clu_sa_email` minus its `.gserviceaccount.com` suffix, which is the login:

```bash
export THEMIS_SQL_CONNECTION_NAME=... THEMIS_SQL_DATABASE=... THEMIS_DB_USER=themis-clu@<project>.iam
```

## The window

**1. Announce it.** Say when the surface goes down and roughly how long for. A curator mid-worksheet loses nothing —
drafts are already saved — but they cannot reach the page while it is closed.

**2. Close the surface.** `curation.roles` is the whole gate: every curation page and API route resolves the caller's
role before it does anything, and a caller with no row is refused. So moving the rows aside closes every route at once,
and putting them back reopens them exactly as they were.

```sql
SET ROLE "themis-deploy@<project>.iam";  -- so the migrator owns the table, as it owns the schema
CREATE TABLE curation.roles_window AS SELECT * FROM curation.roles;
DELETE FROM curation.roles;
```

**3. Deploy.** First read the dev ledger. The runner skips a version it has already recorded, whatever the file now
contains, so a version number an earlier branch claimed is applied as *that* branch wrote it and the difference surfaces
later as a missing column.

```bash
uv run python -m tools.psql -- -c 'SELECT version, name, applied_at FROM schema_migrations ORDER BY version'
```

Then push the chain to `deployed/dev`. The pipeline builds and pushes the images, `pulumi up` rolls each Cloud Run
service onto them, and the migration runner then applies `0012_curation_vocabulary` — the snapshots, and the drop of
`curation.variants.inheritance`. The revision you push must be one that no longer reads that column; the surface's own
change is what makes it so, and deploying this migration ahead of it breaks the variant list in both directions.

**4. Rewrite.** The dry run is the default, and it prints what it would do: one line per member, with the number it was
written under, the number it will carry, and how many rows. Read it before applying — a member you did not expect to see
is a question, not a detail.

```bash
uv run --group curation python -m themis.curation.vocabulary_backfill rewrite
uv run --group curation python -m themis.curation.vocabulary_backfill rewrite --apply --closed-at 2026-08-22T04:00:00+00:00
```

`--closed-at` is the instant you ran step 2, and it is required: a snapshot row written at or after it came from the new
revision and cannot be renumbered again, so the run refuses rather than corrupting it. A draft's write time is its
`updated_at`, and for this run that is what the writer set: the surface's upsert names the column every time, and the
guard rests on that. `0012` also installs a trigger that stamps it on every write, but only after taking its snapshot,
so what the trigger guards is the snapshot of any later migration, not this one's. An assessment's write time is its
submission's `submitted_at`. Re-running `--apply` is safe — the rewrite computes from the snapshot, which nothing
writes, and each update matches the row's current bytes as well as its key, so a second run is a no-op. A run that finds
anything it cannot account for rolls back whole.

**5. Verify.** Reopen only after this passes. Every live row has to be, byte for byte, what the rewrite makes of its
snapshot — so nothing was added, lost, half-written, or touched that should not have been, and no field outside the
renumbered ones moved. What it cannot establish is that the retired numbering was read correctly, since it reads it
through the same table the rewrite wrote with; that is what the census in step 4 is for, and why a member you did not
expect to see is worth stopping on.

```bash
uv run --group curation python -m themis.curation.vocabulary_backfill verify
```

**6. Reopen.**

```sql
-- `CREATE TABLE … AS SELECT` keeps no primary key, so the conflict clause is what makes a second run a no-op.
INSERT INTO curation.roles SELECT * FROM curation.roles_window ON CONFLICT (email) DO NOTHING;
DROP TABLE curation.roles_window;
```

Then open a worksheet and confirm the routing reads what the curator recorded.

## If it goes wrong

Verification failing before step 6 is the case this is written for: leave the surface closed, and restore the two
rewritten tables from their snapshots.

```sql
BEGIN;
-- The restore is not the curator saving: without this, every draft's `updated_at` would date from now.
SET LOCAL curation.backfill_in_progress = 'on';
UPDATE curation.drafts d SET assessment = v.assessment FROM curation.drafts_v1 v
  WHERE d.worksheet_id = v.worksheet_id AND d.workflow_id = v.workflow_id;
UPDATE curation.assessments a SET assessment = v.assessment FROM curation.assessments_v1 v
  WHERE a.submission_id = v.submission_id AND a.workflow_id = v.workflow_id;
COMMIT;
```

What that restores is the *retired* numbering, under a revision that reads the shared one — a rewind to the state step 4
starts from, with the surface still closed, not something to reopen on. Fix what verification found and run step 4
again.

Rolling the web revision back instead is a heavier step. The previous revision selects `curation.variants.inheritance`
by name, and `0012` drops that column, so that revision fails on every read of the variant list until the column is back
— and since this database has no down-migrations, back means a further forward migration re-adding it, not an undo.
`curation.variants_v1` is what that migration refills it from, so what returns is what the manager registered rather
than a value chosen by hand.

## Afterwards

`curation.drafts_v1`, `curation.assessments_v1` and `curation.variants_v1` stay until a later migration drops them.
Leave them through at least one round of curation on the reopened surface: they are the only copy of what was written
under the retired numbering, and of the inheritance the manager registered, and dropping them in the same migration that
created them would defeat the point of taking them. Their columns are asserted against the source tables by
`themis/migrate/tests/test_committed_schema.py`, so the migration that drops them retires that assertion with them.
