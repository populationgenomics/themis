# Design: database migrations

**Related:** [`deployment.md`](deployment.md) (deploy pipeline and ad-hoc branch deploys), [`services.md`](services.md)
(where the migration runner sits in the service tree), [`proto.md`](proto.md) (wire and at-rest protobuf schemas, which
carry no DDL), [`spike-infrastructure.md`](spike-infrastructure.md) (Cloud SQL database and forward-only schema in the
shared dev environment)

## Overview

Multiple backend services share one Cloud SQL Postgres database, and the shared development environment is deployed
directly from unmerged feature branches as well as from `main`. Evolving tables, schemas, and per-service SQL privileges
across those deploys requires a mechanism that uses no password credentials, keeps schema ownership away from runtime
services, and prevents unmerged branches from silently skipping or corrupting each other's schema changes.

The relational schema is defined by a single contiguous sequence of hand-written, forward-only SQL files in
[`themis/migrate/migrations/`](../../themis/migrate/migrations/), applied by the
[`themis/migrate/`](../../themis/migrate/) runner as a deploy-pipeline step after `pulumi up`. The runner connects as
the deploy service account's IAM database user, which owns every schema object and grants narrow table privileges to
runtime service accounts through `${VAR}` login substitutions supplied from Pulumi stack outputs. Before `pulumi up`
rolls any service revision, a read-only preflight checks that every version recorded in the database's migration ledger
matches the SHA-256 content hash of the file in the deployed tree.

Using one hand-written, forward-only sequence gives cross-domain changes a total order and makes re-running the deploy
pipeline safe even when unmerged branches have advanced the shared dev database ahead of `main`. Owning tables as the
deploy identity ensures runtime service accounts cannot bypass their table-level `GRANT`s. Checking content hashes in
both the preflight and the apply step turns branch version collisions and in-place edits of already-applied migrations
into immediate deploy failures with recovery instructions, rather than runtime SQL errors against a mismatched schema.

## Background

Pulumi ([`infra/themis_infra/sql.py`](../../infra/themis_infra/sql.py)) provisions the Cloud SQL Postgres instance, the
application database, each service account's IAM database login, and the GCP project roles required to reach the
instance through the Cloud SQL connector. Beyond attaching the `cloudsqlsuperuser` role to the migrator's login, Pulumi
manages nothing inside Postgres. Schemas, tables, indexes, and the table-level `GRANT` statements that give each runtime
service account its read/write split all arrive through SQL migrations.

Several consumers share this single database: the auth service reads `session_context`, the literature cache writes and
reads `litcache.crosswalk`, and the web application reads and writes the curation tables. Hand-written SQL is the source
of truth for their DDL. Protobuf definitions ([`proto.md`](proto.md)) author wire and at-rest messages; they have no DDL
surface and no migration mechanism of their own.

## Non-goals

- **Down-migrations.** Every schema correction is a new forward migration. Only advancing the deployed schema makes
  re-running a deploy safe and allows the shared development environment to hold migrations from unmerged branches
  without rolling them back when `main` is redeployed.
- **In-database privileges in Pulumi.** Beyond the migrator's `cloudsqlsuperuser` role membership, Pulumi manages only
  logins and instance-level reachability. Every table or schema `GRANT` is a migration.

## Design

### One shared migration sequence

All migrations for the database live in [`themis/migrate/migrations/`](../../themis/migrate/migrations/) as a single
sequence named `NNNN_name.sql`, numbered contiguously from `0001` and applied in ascending version order. Keeping one
sequence and one ledger across the whole database gives cross-domain dependencies a total order: for example,
`0001_session_context.sql` creates the `session_context` table and `0002_grants.sql` grants the auth service `SELECT` on
that table.

During discovery, the runner ([`themis/migrate/migrate.py`](../../themis/migrate/migrate.py)) rejects any filename that
does not match `NNNN_name.sql` with a lowercase slug and any version set that is not contiguous from `1`. In
[`test_migrations.py`](../../themis/migrate/tests/test_migrations.py), two checks run automatically across the committed
migration directory — discovery (`test_committed_migrations_are_discoverable`, catching a version gap or malformed
filename within one tree) and substitution-key pairing against `deploy.yml`
(`test_deploy_provides_every_migration_substitution`) — while per-file tests render `${VAR}` placeholders with stand-in
logins, pin statement counts, and assert statement ordering (adding a new migration's per-file test is on its author).
An applied migration is never edited in place; adding a privilege, column, or table always requires a new migration
file.

Database objects live in `public` unless a domain owns a group of tables that are created, owned, and granted together.
When a domain benefits from stating ownership and schema-level `USAGE` grants once for the domain rather than table by
table, its first migration creates a dedicated schema: the literature cache's crosswalk lives in `litcache` and the
curation worksheet's tables ([`curation-surface.md`](curation-surface.md)) live in `curation`.

To keep tests fast and free of cloud or container dependencies where possible,
[`themis/migrate/`](../../themis/migrate/) separates pure migration logic from the live database adapter. Pure
discovery, `${VAR}` rendering, statement splitting, ledger comparison, and planning have no database driver dependencies
and are unit-tested against an in-memory ledger without starting Postgres. The live adapter in
[`themis/migrate/cloudsql.py`](../../themis/migrate/cloudsql.py) pulls in the Cloud SQL Python Connector and `pg8000`;
[`test_cloudsql.py`](../../themis/migrate/tests/test_cloudsql.py) exercises `CloudSqlLedger`, `read_ledger_read_only`,
and the `--check` entry point against a throwaway Postgres container behind a Docker fixture, and
[`test_committed_schema.py`](../../themis/migrate/tests/test_committed_schema.py) applies the committed SQL set via
`CloudSqlLedger` to a throwaway Postgres container to test schema constraints. Domains whose own tests need live
database tables apply the committed migrations from `themis/migrate/migrations/` rather than hand-writing schema
fixtures, so the tested schema cannot drift from the deployed schema.

### Rendering, splitting, and applying migrations

When the runner executes, it reads the applied migrations from the ledger, verifies that every applied row agrees with
the committed file of the same version, and selects the committed versions absent from the ledger to apply in ascending
order. When no versions are pending, the runner does nothing and exits cleanly, so every deploy runs it unconditionally.

The runner enforces the forward-only rule before applying any SQL: if a pending version is at or below the highest
applied version in the ledger, the runner raises an error and applies nothing. Because the committed set is contiguous
and applied in version order, the ledger stays contiguous as well; a pending version can sit below an applied version
only if someone deleted a row from the ledger by hand.

Before executing a migration, the runner substitutes `${VAR}` placeholders in its SQL text from a JSON substitution map
parsed from the environment ([`themis/migrate/config.py`](../../themis/migrate/config.py)). Placeholders exist for
`GRANT` migrations: a Cloud SQL IAM database login is the service account's email without the `.gserviceaccount.com`
suffix, which is a Pulumi stack output rather than a static constant. A migration therefore writes
`GRANT SELECT ON session_context TO "${AUTH_DB_USER}";`, and the deploy workflow supplies the login in the substitution
map. The runner fails before executing any statement if a required connection variable is unset, if the substitution
JSON is malformed, or if a migration references a `${VAR}` with no value in the map. CI tests parse
[`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) and render every committed migration against the
keys in the workflow's substitution map so a missing substitution key fails in CI rather than during deploy.

Because `pg8000` executes one SQL statement per call, the runner splits the rendered file on top-level semicolons. The
splitter tracks single-quoted string literals and `--` line comments so semicolons inside either (such as inside a
single-quoted `CREATE FUNCTION` or `DO` body) do not split the statement, and it drops segments that contain only
whitespace or comments so a trailing comment after the final semicolon is not sent to Postgres as an empty query. The
cost of keeping the splitter this small is that `/* ... */` block comments and `$$ ... $$` dollar-quoted strings are not
modelled: a migration using either will split at an internal semicolon without a parser error.

### Checking the ledger against the tree

Comparing only version numbers between the database ledger and the directory tree works when every deploy comes from a
single linear branch. The shared dev environment, however, is deployed directly from feature branches via `deployed/dev`
before those branches merge to `main` ([`deployment.md`](deployment.md)). Under branch deploys, comparing version
numbers alone fails when unmerged branches claim the same version number or when an author edits an already-deployed
migration in place:

1. **Two branches claim the same version number.** Suppose branch A deploys `0013_variant_notes.sql`, which adds a
   column to a table. Branch B, cut before A merged, then deploys its own `0013_case_flags.sql`. A runner that checked
   only version numbers would see version `13` already in the ledger and skip B's migration. B's service code would then
   query a column that was never created, failing at runtime with Postgres error `42703` (undefined column).
1. **One branch edits an already-deployed migration in place.** Suppose branch B deploys `0013_case_flags.sql` adding
   `flagged boolean`. Review asks for `flagged_at timestamptz` instead, so the author edits `0013_case_flags.sql` in
   place and redeploys. Because version `13` is already recorded under the same filename, a runner that checked only
   version numbers and filenames would skip the edited file, leaving B's code querying a nonexistent `flagged_at`
   column.

To catch both cases, the ledger records the SHA-256 content hash of each migration file as committed on disk, before
`${VAR}` placeholder rendering so that the hash does not depend on environment-specific database logins. Before applying
any pending migrations, the runner compares every row in the ledger against the committed file of the same version:

- **Row with a recorded `sha256`:** the committed file's SHA-256 hash must equal the recorded hash. If a file is renamed
  without changing its contents, the check still passes because the database holds the exact SQL in the file. If the
  hash differs, the runner raises an error naming the version, the committed file, and (when the recorded name also
  differs) the migration name recorded in the ledger.
- **Legacy row with `sha256 IS NULL`:** rows applied before the ledger stored content hashes have only their recorded
  slug name, so the committed file's name must match the recorded name.
- **Applied version absent from the deployed tree:** if the ledger holds version `0013` from branch A and the next
  deploy runs from `main` (which stops at `0012`), the check passes. Because migrations are forward-only, refusing the
  deploy would not undo `0013` and would block redeploying a known-good ref on `main`. Branch A's leftover migration
  causes a conflict only when another tree introduces its own `0013`.

The ledger check runs twice during every deploy in [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml):

```
  deploy.yml start
        │
        ▼
┌────────────────────────────────────────────────────────┐
│ 1. Preflight: python -m themis.migrate --check         │
│    (reads previous deploy's stack outputs, read-only)  │
└───────┬───────────────────┬────────────────────────────┘
        │ exit 0            │ exit 3 (ledger unreadable:
        │ (matches or       │  broken login / unreachable DB)
        │  new stack)       ▼
        │             Emit warning, continue so pulumi up
        │             can repair the login/infra
        ├───────────────────┘
        ▼
┌────────────────────────────────────────────────────────┐
│ 2. Build & push images, then run `pulumi up`           │
│    (new Cloud Run revisions go live; DB users updated) │
└───────┬────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│ 3. Migrate: python -m themis.migrate                   │
│    (acquires pg_advisory_lock, re-checks ledger        │
│     authoritatively, applies pending NNNN_name.sql)    │
└────────────────────────────────────────────────────────┘
```

Running the check in a read-only preflight before `pulumi up` stops a colliding or edited migration before any new
service revision goes live. The preflight opens a read-only transaction and never creates or alters the
`schema_migrations` table; a database without the table (or a stack with no outputs on its first bring-up) reads as
having applied nothing.

The preflight and the post-`pulumi up` migration step differ in how they treat a ledger they cannot read:

- **Exit code `1` (ledger finding — hash/name mismatch or forward-only gap):** stops both the preflight and the
  migration step immediately.
- **Exit code `3` (`--check` only: ledger unreadable — connection, authentication, or permission error):** `--check`
  alone has this exit code so the preflight can distinguish *drifted* from *unreachable* and log a warning rather than
  aborting (if the post-`pulumi up` apply step still cannot connect, it raises an unhandled error and fails hard with
  exit code `1`). The preflight connects using the *previous* deploy's stack outputs, which may themselves be what is
  broken. If an ad-hoc branch deploy broke the migrator's login, the repair is to redeploy a known-good ref whose
  `pulumi up` restores the login; a preflight that failed hard on an unreadable ledger would block that repair before
  `pulumi up` could run.

Because the preflight can warn past an unreadable ledger (or someone can modify the database out of band between the
preflight and the migration step), the post-`pulumi up` check under the advisory lock is authoritative. If a mismatch is
caught only at that second check, `pulumi up` has already rolled the services to their new revisions, no pending
migrations are applied, and the new service code runs against the old schema until a deploy succeeds.

#### Recovering from a ledger mismatch

While a tree and the database ledger disagree, every deploy of that tree stops at the preflight with nothing changed.
How to clear the failure depends on whether two branches collided on a version number or a single migration was edited
after being applied:

- **Two branches collided, and branch A (whose migration is in dev's ledger) merges first:** rebase branch B onto
  `main`, which now contains A's `0013`, and renumber B's migration to `0014_case_flags.sql`. B's tree now matches the
  ledger at `0013` and treats `0014` as pending.
- **Two branches collided, and branch B merges first:** `main` now holds B's `0013`, which contradicts dev's ledger, so
  deploys of `main` stop at the preflight until dev's database is reconciled by hand. Drop the objects created by A's
  migrations (unless A's renumbered migrations will tolerate finding them already present) and delete A's rows from
  `schema_migrations` from version `13` upward. That leaves B's `0013` pending for the next deploy of `main`, and branch
  A renumbers its migrations past B's when it rebases.
- **An applied migration was edited on an unmerged branch:** restore the original file on the branch to the exact bytes
  that were deployed, and put the schema change into a new numbered migration after it.
- **An applied migration was edited and already merged to `main`:** either restore the original file on `main` and move
  the edit into a new migration (preserving the rule that applied files never change), or reconcile dev by hand by
  applying the SQL delta as the migrator role (so the new objects have the same owner as the rest of the schema) and
  updating the row's `sha256` in `schema_migrations` to match the edited file. Manual reconciliation in dev is faster
  when dev is the only environment that applied the original file.

### The Cloud SQL ledger

The live ledger in [`themis/migrate/cloudsql.py`](../../themis/migrate/cloudsql.py) tracks applied migrations in the
`schema_migrations` table (`version` primary key, `name`, `sha256`, `applied_at`). Because the runner reads `sha256`
during the ledger check before applying any numbered migration, neither the table nor the `sha256` column can be created
by a numbered migration. The `sha256` column is nullable so that rows recorded before content hashing was added remain
valid.

On its first read in apply mode, the runner inspects the existing columns of `schema_migrations` in the Postgres catalog
and issues `CREATE TABLE` or `ALTER TABLE ... ADD COLUMN sha256 text` only when the table or column is missing. The
obvious shortcut — running `ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS sha256 text` unconditionally on every
run — breaks when the migrator accesses the ledger through a granted role rather than owning the table directly, because
Postgres verifies table ownership before evaluating `IF NOT EXISTS` and takes an `ACCESS EXCLUSIVE` lock on the table.
Checking the catalog first ensures that once the table and column exist, normal runs issue no DDL against the ledger.

Each migration is recorded by running its SQL statements and inserting its `schema_migrations` row inside a single
Postgres transaction. Because Postgres DDL is transactional, a migration that fails on any statement rolls back its
entire schema change along with its version row, leaving the migration cleanly re-runnable on the next deploy.

Across the whole apply run, the runner holds a single IAM-authenticated connection
([`themis/common/sql.py`](../../themis/common/sql.py)) and acquires a session-level `pg_advisory_lock` on a fixed key.
If two deploys overlap, one applies while the other blocks on the lock and then sees nothing pending. Because the lock
is session-scoped, Postgres releases it automatically when the connection closes, including on a process crash. Because
the Cloud SQL connector dials the Cloud SQL Admin API, `apply_migrations`, the apply path of `__main__.py`, the
`connect` / `iam_connect` connector lifecycle, `pg_advisory_lock` serialization across concurrent sessions, and the
workflow preflight step in `deploy.yml` have no local container test — they are verified by deploying to `dev`.

### How it runs

The deploy workflow ([`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml)), triggered from `main` or
`deployed/<env>`, authenticates as `themis-deploy@` via Workload Identity Federation, runs the read-only ledger
preflight (`python -m themis.migrate --check`), builds and pushes the container images, runs `pulumi up`, and then runs
`python -m themis.migrate` using the stack outputs from `pulumi up`.

Running migrations *after* `pulumi up` is required because `GRANT` migrations target IAM database users that `pulumi up`
provisions; a service's database login must exist before a migration can grant privileges to it.

The cost of this ordering falls on service rollouts: `pulumi up` rolls every Cloud Run service to its new container
image before the migration step runs. Every new revision therefore serves traffic against the *previous* database schema
until the migration step completes — or, if the migration step fails, until the next successful deploy. Consequently,
migrations must be additive by default, and application code reading a new table or column must tolerate its absence for
at least one deploy window.

An expand-and-contract sequence across multiple deploys (add the new column, write both, migrate readers, then drop the
old column in a later deploy) requires maintaining a dual-schema compatibility path in every reader and writer, so it is
worth declining while an environment is still disposable and worth paying once it is not. A single-step destructive
migration is allowed only when the environments it will run against hold no data worth keeping and have no external
users to disrupt, and when the owning design doc records which reads and writes fail during the deploy window and until
when (for example, replacing `analyses.prompt` with `analyses.inputs NOT NULL` in
[`analysis-scenarios.md`](analysis-scenarios.md), or the curation schema transitions in
[`curation-surface.md`](curation-surface.md)).

### Identity and the ownership bootstrap

The migrator logs into Postgres as the deploy service account's own Cloud SQL IAM database user (`themis-deploy@`),
distinct from every runtime service account. In Postgres, a table's owner bypasses all table-level `GRANT` restrictions.
Having the deploy identity own every schema and table ensures that runtime service accounts cannot impersonate the owner
and therefore hold only the privileges explicitly granted to them in migrations. This separation enforces least
privilege between runtime services in the database; it does not defend against a compromised deploy identity, which can
already modify migrations and IAM bindings.

A freshly created Cloud SQL IAM database user has `LOGIN` and no `CREATE` privilege on `public`. Pulumi
([`infra/__main__.py`](../../infra/__main__.py) and [`infra/themis_infra/sql.py`](../../infra/themis_infra/sql.py))
therefore attaches `cloudsqlsuperuser` to the migrator's `sql.User` resource via the Cloud SQL Admin API, which is the
password-free way to grant an IAM user `CREATE` on `public`. The cost is that `cloudsqlsuperuser` is broader than schema
creation alone: it also carries `CREATEDB` and `CREATEROLE`, allowing the migrator login to create databases and roles.

Adding a new database-backed service requires provisioning its IAM database login and connector roles in Pulumi, adding
a numbered `GRANT` migration parameterized by a `${VAR}` placeholder, and wiring the stack's exported login into the
migration step's substitution map in [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml); a service's
rights are the `GRANT`s in the migration that introduced them, and a grant that has not shipped is a migration nobody
has written yet.

## Alternatives considered

- **Alembic.** Provides a general revision graph, autogeneration against SQLAlchemy models, downgrade paths, and full
  SQL execution without a custom statement splitter. We did not adopt it because hand-written SQL is the sole source of
  truth (there are no SQLAlchemy models), down-migrations are a non-goal, and a single linear sequence needs only a
  version table and an apply loop. Revisit if migrations need block comments or dollar-quoting, if the statement
  splitter grows beyond single-quoted strings and line comments, or if branching or autogeneration becomes useful.
- **Library-based statement splitting (`sqlparse` or equivalent).** Parses full SQL lexical syntax (including block
  comments and dollar-quoting) without maintaining a custom state machine. We kept the hand-written splitter because a
  full SQL parser brings a third-party dependency modelling far more grammar than top-level semicolon splitting needs,
  while the current splitter's scope and limits are small and directly tested.
- **Per-domain migration sequences.** Letting each domain package maintain its own numbered migration directory would
  allow domains to add migrations without colliding on version numbers across branches. However, cross-domain statements
  (such as a `GRANT` on another domain's table or a cross-domain foreign key) would have no defined execution order, and
  the ledger would require per-domain namespaces.
- **Running migrations in a Cloud Run Job instead of a CI step.** Moving execution into a Cloud Run Job would mean the
  GitHub Actions runner triggers the job rather than opening a database connection itself. Because the deploy service
  account in CI already provisions Cloud SQL and IAM, a compromised CI runner could grant itself a database login or
  replace the Job's container image anyway, so a Cloud Run Job adds operational indirection without changing the trust
  boundary.
- **Granting schema ownership through a Pulumi Postgres provider.** Issuing `GRANT CREATE ON SCHEMA public` or
  `ALTER SCHEMA public OWNER TO` as a Pulumi resource would avoid granting `cloudsqlsuperuser` to the migrator. However,
  the Pulumi Postgres provider dials the instance directly with a password login and does not speak the Cloud SQL
  connector protocol. Because the instance has no authorized networks and requires the connector, using the provider
  would require running a Cloud SQL Auth Proxy alongside `pulumi up` and storing a password secret that IAM
  authentication exists to avoid.
- **Migrating as the instance's built-in admin user.** The built-in `postgres` user already has superuser privileges
  without attaching `cloudsqlsuperuser` to an IAM user, but it authenticates with a shared password, defeating IAM
  database authentication and losing per-identity audit trails.
