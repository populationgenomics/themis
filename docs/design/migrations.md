# Design: database migrations

**Status:** current **Related:** [`deployment.md`](deployment.md) (deploy identity and the pipeline this runs in),
[`services.md`](services.md) (where `themis.migrate` sits in the `themis` tree), [`proto.md`](proto.md) (proto is
wire/at-rest model authoring, not DDL), [`spike-infrastructure.md`](spike-infrastructure.md) §7 (forward-only schema in
the shared dev environment)

## Overview

The relational schema of the Cloud SQL Postgres database is a set of hand-written, forward-only SQL files under
`themis/migrate/migrations/`, applied by a small runner (`themis.migrate`) as a deploy-pipeline step after `pulumi up`.

## Background

Pulumi provisions the instance, the application database, each service account's IAM DB-user login, and the project
roles needed to reach the instance — and, beyond the migrator's role membership (below), nothing inside the database.
Tables, indexes, schemas, and the table privileges that give each runtime SA its read/write split all arrive through
migrations.

One database is shared by several consumers (the auth service reads `session_context`; litcache mints into
`litcache.crosswalk`), and hand-written SQL is the source of truth for their DDL. Proto authors the wire and at-rest
models; it has no DDL surface and, being additive-only, no migration concept of its own.

## Non-goals

- **No down-migrations.** A correction is a new forward migration. The deployed schema only ever advances, which is what
  makes re-running the deploy safe and what the shared dev environment relies on.
- **No in-database privileges in Pulumi**, beyond the migrator's `cloudsqlsuperuser` role membership. Pulumi owns logins
  and instance-level reachability; every table GRANT is a migration.

## Design

### Layout

```
themis/migrate/
  __init__.py
  migrate.py     discover / render / split_statements / plan / run, the Ledger port, InMemoryLedger
  cloudsql.py    CloudSqlLedger, apply_migrations and the read-only check (the live path)
  config.py      the Cloud SQL and substitution inputs, read from the environment
  __main__.py    entry point: load config, then apply, or with --check only check the ledger
  migrations/    the committed NNNN_name.sql set
  tests/
```

`migrate.py` is pure and dependency-free. Importing `cloudsql.py` pulls the Cloud SQL connector and pg8000, so outside
`__main__` only the Docker-gated ledger test imports it; the hermetic tests import `migrate.py` and `config.py`.

### The migration set

One shared sequence for the whole database: `NNNN_name.sql`, forward-only, contiguous from `0001`, ordered by version.
One ledger over one database gives cross-domain dependencies — `0002_grants.sql` granting over the table `0001` creates
— a total order.

`discover` rejects a filename that is not `NNNN_name.sql` (lowercase slug) and any version set that is not contiguous
from 1. The unit tests run the committed set through it, so a gap or a duplicate version fails CI rather than reordering
silently. CI sees a duplicate only once both files are in one tree, though. Two branches that each add their own `0013`
are each valid alone, and their collision first shows up in a database both were deployed to; the runner's ledger check
(below) catches it there. An applied migration is never edited, and the same check catches an edit wherever the ledger
recorded the file's hash. A new privilege, column, or table is a new file.

Objects land in `public` unless a domain owns a schema of its own. A schema is worth taking where a domain's tables are
granted, owned and changed as one, so that all three can be stated about the domain rather than table by table:
litcache's crosswalk sits in a `litcache` schema and the curation worksheet's tables
([`curation-surface.md`](curation-surface.md) §Storage) in a `curation` schema, each created by the first migration that
needs it.

### Applying

`run(migrations, ledger, substitutions)` reads the applied migrations from the ledger and checks them against the
committed set before it applies anything (the next section). It then takes the committed versions absent from the ledger
and applies them ascending. Forward-only is enforced here: a pending version at or below the highest applied version
raises. The committed set is contiguous and the runner applies it in order, so the ledger stays contiguous as well. A
pending version can therefore sit below an applied one only where someone has removed a row from the ledger by hand, and
the forward-only check guards against that. Two branches that claim the same version produce no pending version, since
that version is already applied; the ledger check catches their collision instead. Nothing pending is a no-op, so every
deploy can run the runner unconditionally.

`render` substitutes `${VAR}` placeholders and raises on a placeholder with no value. It exists for the GRANT
migrations: an IAM DB-user login is the service account's email minus the `.gserviceaccount.com` suffix, a Pulumi output
rather than a constant, so `0002_grants.sql` grants to `"${AUTH_DB_USER}"` and the deploy step supplies the value.

`split_statements` splits the rendered SQL on top-level `;` because pg8000 executes one statement per call. It tracks
single-quoted strings and `--` line comments so a `;` inside either is not a separator, and drops segments carrying no
executable SQL (a trailing comment after the last `;` would otherwise be sent as an empty query, which Postgres
rejects). Block comments and dollar-quoting are not modelled: a migration using either splits wrongly, and the splitter
has no error path, so nothing detects it — see Alternatives considered for when that limit should be lifted.

`Ledger` is the port. `applied_migrations()` returns each applied version with the name and content hash it was recorded
under, and `record(migration, sql)` must apply the SQL and record all three atomically. `InMemoryLedger` backs the
hermetic unit tests; `CloudSqlLedger` is the live implementation.

### Checking the ledger against the tree

A version number is unique only within one tree. The shared dev environment is deployed from branches before they merge
([`deployment.md`](deployment.md)), so dev's ledger can record, under a version, a migration other than the one the
deployed tree has. That happens in two ways.

The first takes two branches. Suppose branch A deploys `0013_variant_notes.sql`, which adds a column to a table. Branch
B, cut before A merged, then deploys its own `0013_case_flags.sql`. A runner that compared version numbers alone would
find 13 applied and skip B's file. B's code would then query a column that B's migration was meant to create and never
did, and the failure would surface at runtime as Postgres error 42703 (undefined column), far from its cause.

The second takes only one. Suppose B's `0013_case_flags.sql` adds a column `flagged boolean` and B deploys it. Review
then asks for `flagged_at timestamptz` instead, so B edits the file and deploys again. Version 13 is already applied, so
the runner applies nothing, and B's code queries a `flagged_at` column that does not exist. The file kept its name, so
comparing names alone would not catch this.

So the runner compares every applied row with the committed file of the same version. The ledger records the SHA-256 of
each file it applies, and where a row carries one, the committed file's hash must match it. The hash covers the file as
committed, before `render` fills in its placeholders, so it does not depend on the logins an environment substitutes.
Rows applied before the ledger kept hashes have none, and for those the names must match. A file renamed without any
other change still passes where a hash is recorded, since the database holds exactly what the file says.

The error lists every version that disagrees. For a row with a hash it reports that the content differs, and where the
recorded name differs as well, it names the file the ledger recorded. In the first example, the content of
`0013_case_flags.sql` differs from the file the ledger recorded as `0013_variant_notes.sql`. In the second, it differs
from the file applied under its own name. Only a row without a hash is reported by name alone, because the name is all
such a row has to compare.

The check runs twice in every deploy. A preflight runs it read-only before `pulumi up`, connecting with the settings the
stack exported on its previous deploy, so a mismatch stops the deploy before any new revision goes live. It writes
nothing to the database, not even the ledger table: a database without one reads as having applied nothing. The
preflight skips only a stack that has no outputs yet, which has never been deployed and so has no database to check. The
runner then repeats the check before it applies anything, under its advisory lock, and that check is authoritative,
because the ledger can change between the two.

The two runs differ in what stops a deploy. The runner fails on anything that goes wrong. The preflight stops the deploy
only on a finding, which is a row that disagrees with its committed file or a forward-only violation. If the preflight
cannot connect to the database, authenticate, or read the ledger, it reports a warning that names the failure, and the
deploy goes on to `pulumi up`. The preflight connects with the settings of the previous deploy, and those settings can
be the thing that is broken. Suppose an ad-hoc deploy from a branch changed the migrator's login, and the new login does
not work. The repair is to redeploy a good ref, whose `pulumi up` restores the login. A preflight that failed whenever
it could not read the ledger would stop that redeploy before `pulumi up`, so dev could not be repaired through the
pipeline at all. The entry point ([`__main__.py`](../../themis/migrate/__main__.py)) exits with one status for a finding
and another for a ledger it could not read, and the workflow maps those statuses. It does not classify a failure by
reading its output.

So a mismatch reaches the migrate step in two ways. The preflight may have been unable to read the ledger, or the ledger
may have changed after the preflight read it, by a hand edit or a migration run outside the pipeline, since the pipeline
serialises its own deploys. Either way the mismatch fails the migrate step. It then leaves what any other migrate
failure leaves (How it runs): `pulumi up` has already put the new revisions live, none of the tree's pending migrations
is applied, and the new code runs against the old schema until a deploy succeeds.

An applied version with no committed file passes the check. That is the state after A's deploy when `main`, which still
ends at `0012`, is deployed next: dev keeps A's `0013`, and `main` has nothing pending. Whatever the leftover did to
`main`'s code it has already done. Refusing the deploy would not undo it, since migrations are forward-only, and it
would block deploys that work, such as redeploying a good ref after a branch's migration
([`deployment.md`](deployment.md)). The leftover causes a collision only once the deployed tree gains a `0013` of its
own, and the check catches it then.

While the ledger and a tree disagree, every deploy of that tree stops at the preflight with nothing changed. A stopped
deploy whose error names both migrations is quicker to diagnose than a runtime error in code whose schema silently
differs from the one it was written against. How the disagreement clears depends on which of the two colliding branches
merges first:

- If A, whose migration dev's ledger holds, merges first, B rebases onto `main`, which now carries A's `0013`, and
  renumbers its own migration past it to `0014_case_flags.sql`. B's tree then agrees with the ledger at 13, and its own
  migration is pending.
- If B merges first, `main` holds B's `0013`, which dev's ledger contradicts, so no deploy of `main` gets through until
  dev's database is reconciled by hand. Someone decides whether what A's migrations created stays or goes, dropping it
  unless A's renumbered migrations will tolerate finding it already there. They then delete A's rows from 13 up, since A
  may have applied several migrations past `0012`. That leaves B's `0013` pending, and A renumbers its migrations past
  B's when it rebases.

An edited migration, the second case, is fixed on its own branch: restore the file as it was deployed, and put the
change in a new migration after it.

The edit may already have merged, so that no branch is left to restore the file on, and `main` itself disagrees with
dev's ledger. There are two ways out. Someone can reconcile dev by hand: apply the difference between the deployed file
and the edited one as the migrator, so the new objects have the same owner as the rest, and then set the row's `sha256`
to the edited file's hash. Or a change on `main` can restore the file as it was deployed and move the edit into a new
migration. The second keeps every applied file unchanged, which is the rule the check enforces. The first is quicker
when dev is the only database that applied the original.

### The Cloud SQL ledger

`CloudSqlLedger` tracks applied migrations in `schema_migrations` (`version` PK, `name`, `sha256`, `applied_at`). `name`
holds the slug after the number, `session_context` for `0001_session_context.sql`, and `sha256` the content hash the
ledger check compares. The column cannot arrive through a migration, because the runner reads it in its check before it
applies anything. It is nullable, since rows applied before it existed have no hash.

The runner's first read creates the table where there is none, and adds the `sha256` column to a ledger that predates
it. It issues each statement only when its read of the table's columns shows that piece missing. The obvious shortcut,
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on every run, would demand more than it does. Postgres checks that the caller
owns the table before it evaluates `IF NOT EXISTS`, and the statement takes an exclusive lock on the table. A migrator
that reaches the ledger through a granted role, without owning it, could then not apply anything, and every run would
lock the ledger against its readers. Once the table and the column exist, a run issues no DDL on the ledger.

Each `record` runs the migration's statements and inserts its version row in one transaction — Postgres DDL is
transactional — so a migration that fails partway records no version and the next run retries it whole.

`apply_migrations` holds a single IAM-authed connection (`themis.common.sql.iam_connect`, through the Cloud SQL
connector) for the entire run and takes a session-level `pg_advisory_lock` on a fixed key. Two overlapping deploys
therefore serialize: one applies, the other blocks and then finds nothing pending. The lock is session-level, so it is
released when the connection closes, including on a crash.

### Configuration

All input is environment, and every required value is fail-loud: `THEMIS_SQL_CONNECTION_NAME` / `THEMIS_SQL_DATABASE` /
`THEMIS_DB_USER` (the migrator's login), plus `THEMIS_MIGRATE_SUBSTITUTIONS`, a JSON object of string to string carrying
the GRANT logins. Absent substitutions mean the empty map; a malformed value raises rather than rendering a partial
GRANT.

### How it runs

`.github/workflows/deploy.yml`, run from a deployable ref (`main` or `deployed/<env>`), authenticates as
`themis-deploy@` through Workload Identity Federation, runs the ledger preflight (`python -m themis.migrate --check`),
builds and pushes the service images, runs `pulumi up`, then runs `uv run --group migrate python -m themis.migrate` with
the stack's outputs as environment: `sql_connection_name`, `sql_database`, and `migrator_db_user`, plus every runtime
SA's exported `*_db_user` login folded into `THEMIS_MIGRATE_SUBSTITUTIONS` under the `${VAR}` name its GRANT migration
uses. The order is load-bearing in one direction — a GRANT's target DB user is a Pulumi resource, so it must exist
before the migration granting to it runs.

The consequence in the other direction is accepted, not avoided: `pulumi up` rolls every Cloud Run service to its new
image before the migrations run, so a new revision is live against the previous schema until the migrate step completes
— or, if that step fails, until the next successful deploy. Every migration must therefore be additive, and code reading
a new table or column must tolerate its absence for at least one deploy window.

A destructive migration is allowed only where that window costs nothing real: the environments it will run in hold no
data worth keeping and no users to fail, and the migration's own doc names which reads and writes break and until when.
The expand/contract shape the rule otherwise forces — write both columns, tolerate the absent one, drop it a deploy
later — is a compatibility mode in every reader and writer of the column, so it is worth declining while an environment
is still disposable and worth paying once it is not. `analysis-scenarios.md` §Storage is the worked instance:
`0008_analysis_inputs` drops a `NOT NULL` column and deletes every row, and records the failing window it accepts.

### Identity and the ownership bootstrap

The migrator is the deploy service account's own Cloud SQL IAM DB user, distinct from every runtime SA. A table's owner
bypasses GRANTs, so owning the schema from an identity no runtime SA can impersonate is what makes the table-level
GRANTs the whole of a service's rights. This is a database-level least-privilege split, not a defence against a
compromised deploy identity, which can rewrite the migrations anyway.

A freshly created Cloud SQL IAM user has a login and nothing else — no `CREATE` anywhere. `infra/__main__.py` therefore
attaches `cloudsqlsuperuser` as a `database_roles` entry on the migrator's `sql.User`, applied through the Admin API;
that is the password-free way to give an IAM user `CREATE` on `public`. It is broader than that one need — the role also
carries `CREATEDB` and `CREATEROLE`, so the migrator can mint databases and roles.

Adding a service to the database is: attach its login and connect roles in Pulumi, add a grants migration keyed on a
`${VAR}`, and pass that login in the deploy step's substitution map.

### Testing

`migrate.py` and `config.py` are unit-tested against synthetic SQL and `InMemoryLedger`: `discover`'s ordering and its
rejections (malformed filename, version gap), `render`'s substitution and its raise on an unsupplied placeholder,
`split_statements` on semicolons inside strings and comments and on a trailing comment-only segment, `run`'s ordering,
idempotency, substitution and forward-only rejection, the ledger check in each of its outcomes, and every config-loader
path.

Against the *committed* migrations, one test asserts the discovered roster — every name in version order — so a file
added, renamed, or misnumbered fails there rather than at deploy. Beyond that the coverage is per file: a migration
carrying `${VAR}` placeholders gets a test rendering it with stand-in logins and asserting the GRANT text it produces;
one without gets its split asserted alone. Both pin a statement count, a value no legitimate change moves since applied
migrations are never edited. Only the roster assertion is automatic — a new migration's own test is on its author.

Two Docker-gated paths (behind the shared `docker_daemon` fixture) reach real Postgres, each standing up its own
throwaway instance. `CloudSqlLedger` is driven directly on a raw pg8000 connection: the ledger table auto-creates and
gains the hash column, a successful `record` commits and reads back with its hash, and a migration whose statements fail
mid-way commits nothing — no version row and no partial DDL survive. A migrator that reaches a current ledger through a
granted role, without owning it, applies over it. The read-only check runs against a database with no ledger and against
one without the hash column, and leaves both unchanged, and Postgres rejects a write attempted inside its transaction.
The `--check` entry point runs there too, with only the connector swapped out: it exits with the finding status on a
mismatch and on a forward-only violation, and with the unreadable status on a failed login and on a login without read
access to the ledger. Separately, a domain whose tests need live tables applies its own migration through
`discover`/`render`/`split_statements` rather than hand-writing the schema in a fixture, so the tested schema cannot
drift from the deployed one.

`apply_migrations`, the apply path of `__main__`, and the preflight step in the workflow have no test. The advisory-lock
serialization, the connector/connection lifecycle around `iam_connect`, the `run` → `CloudSqlLedger` wiring, and the
preflight's reading of the stack outputs are therefore established only by a deploy — as is the execution of every
migration no domain test applies for itself.

## Alternatives considered

**alembic.** Rejected for now: what is used here is version ordering, a ledger table, and an apply, while alembic brings
a revision graph, autogeneration against SQLAlchemy models (which do not exist here; SQL is the source of truth), and
downgrade paths that are ruled out. Revisit if any of these hold:

- a migration needs block comments or dollar-quoting, which the splitter would have to grow to parse;
- the splitter grows meaningfully beyond its current narrow scope;
- autogenerate, downgrade/branching, or a richer version graph becomes wanted — all of which alembic gives free.

**A library-based statement split (sqlparse or equivalent).** Removes the hand-written parser at the cost of a
dependency modelling far more SQL than the split needs. The current splitter is directly tested and its limits are
declared; it is the second tripwire above.

**Per-domain migration sets.** One sequence per owning package would let domains version independently, but cross-domain
statements (a GRANT over another domain's table, a foreign key) then have no defined order, and the ledger needs a
per-set namespace.

**A Cloud Run Job instead of a CI step**, so CI would only trigger migrations rather than hold a DB login. It buys
nothing: the deploy identity already provisions Cloud SQL and IAM, so a compromised runner can grant itself a login or
rewrite the Job regardless.

**A Pulumi-issued ownership grant** — `GRANT CREATE ON SCHEMA public` / `ALTER SCHEMA public OWNER TO` as a one-time
step in the Pulumi program. Expressing SQL-level grants in Pulumi needs a Postgres provider, which dials the instance
directly with a password login; the instance has no authorized networks, so the only route in is the Cloud SQL
connector, which that provider does not speak. It would take a proxy process running beside `pulumi up` plus the stored
password IAM auth exists to remove. The Admin API's `database_roles` reaches the same state with credentials the deploy
already holds.

**Migrating as the instance's built-in admin user.** Password-authenticated and shared, so it defeats IAM database auth
and leaves no per-identity audit trail. Granting `cloudsqlsuperuser` to the migrator's IAM user gets the same `CREATE`
rights while keeping the login federated.

## Implementation state

Shipped: the runner, the Cloud SQL ledger, the deploy-pipeline step that applies the set, and the preflight that checks
the ledger before `pulumi up`. What the schema currently holds is `themis/migrate/migrations/` itself, and a service's
rights are the GRANTs in the migration that introduced them; a grant that has not shipped is a migration nobody has
written yet.
