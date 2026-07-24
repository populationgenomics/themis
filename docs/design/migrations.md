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
  migrate.py     discover / render / split_statements / run, the Ledger port, InMemoryLedger
  cloudsql.py    CloudSqlLedger + apply_migrations (the live path)
  config.py      the Cloud SQL and substitution inputs, read from the environment
  __main__.py    entry point: load config, apply
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
from 1. The unit tests run the committed set through it, so a gap or a duplicate version (two branches both claiming
`0004`) fails CI rather than reordering silently; the deploy is the backstop. An applied migration is never edited; a
new privilege, column, or table is a new file.

Objects land in `public` unless a domain owns a schema of its own — litcache's crosswalk sits in a `litcache` schema
created by its migration.

### Applying

`run(migrations, ledger, substitutions)` takes the versions absent from `ledger.applied_versions()` and applies them
ascending. Forward-only is enforced here: a pending version at or below the highest applied version raises, which
catches a migration that merged behind one already deployed. Nothing pending is a no-op, so every deploy can run the
runner unconditionally.

`render` substitutes `${VAR}` placeholders and raises on a placeholder with no value. It exists for the GRANT
migrations: an IAM DB-user login is the service account's email minus the `.gserviceaccount.com` suffix, a Pulumi output
rather than a constant, so `0002_grants.sql` grants to `"${AUTH_DB_USER}"` and the deploy step supplies the value.

`split_statements` splits the rendered SQL on top-level `;` because pg8000 executes one statement per call. It tracks
single-quoted strings and `--` line comments so a `;` inside either is not a separator, and drops segments carrying no
executable SQL (a trailing comment after the last `;` would otherwise be sent as an empty query, which Postgres
rejects). Block comments and dollar-quoting are not modelled: a migration using either splits wrongly, and the splitter
has no error path, so nothing detects it — see Alternatives considered for when that limit should be lifted.

`Ledger` is the port: `applied_versions()` plus `record(migration, sql)`, which must apply the SQL and mark the version
atomically. `InMemoryLedger` backs the hermetic unit tests; `CloudSqlLedger` is the live implementation.

### The Cloud SQL ledger

`CloudSqlLedger` tracks applied versions in `schema_migrations` (`version` PK, `name`, `applied_at`), created
idempotently on the first read. Each `record` runs the migration's statements and inserts its version row in one
transaction — Postgres DDL is transactional — so a migration that fails partway records no version and the next run
retries it whole.

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

`.github/workflows/deploy.yml`, on push to `main`, authenticates as `themis-deploy@` through Workload Identity
Federation, builds and pushes the service images, runs `pulumi up`, and then runs
`uv run --group migrate python -m themis.migrate` with the stack's outputs as environment: `sql_connection_name`,
`sql_database`, and `migrator_db_user`, plus every runtime SA's exported `*_db_user` login folded into
`THEMIS_MIGRATE_SUBSTITUTIONS` under the `${VAR}` name its GRANT migration uses. The order is load-bearing in one
direction — a GRANT's target DB user is a Pulumi resource, so it must exist before the migration granting to it runs.

The consequence in the other direction is accepted, not avoided: `pulumi up` rolls every Cloud Run service to its new
image before the migrations run, so a new revision is live against the previous schema until the migrate step completes
— or, if that step fails, until the next successful deploy. Every migration must therefore be additive, and code reading
a new table or column must tolerate its absence for at least one deploy window.

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
idempotency, substitution and forward-only rejection, and every config-loader path.

Against the *committed* migrations, one test asserts the discovered roster — every name in version order — so a file
added, renamed, or misnumbered fails there rather than at deploy. Beyond that the coverage is per file: a migration
carrying `${VAR}` placeholders gets a test rendering it with stand-in logins and asserting the GRANT text it produces;
one without gets its split asserted alone. Both pin a statement count, a value no legitimate change moves since applied
migrations are never edited. Only the roster assertion is automatic — a new migration's own test is on its author.

Two Docker-gated paths (behind the shared `docker_daemon` fixture) reach real Postgres, each standing up its own
throwaway instance. `CloudSqlLedger` is driven directly on a raw pg8000 connection: the ledger table auto-creates, a
successful `record` commits, and a migration whose statements fail mid-way commits nothing — no version row and no
partial DDL survive. Separately, a domain whose tests need live tables applies its own migration through
`discover`/`render`/`split_statements` rather than hand-writing the schema in a fixture, so the tested schema cannot
drift from the deployed one.

`apply_migrations` and `__main__` have no test. The advisory-lock serialization, the connector/connection lifecycle
around `iam_connect`, and the `run` → `CloudSqlLedger` wiring are therefore established only by the deploy apply — as is
the execution of every migration no domain test applies for itself.

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

Shipped: the runner, the Cloud SQL ledger, and the deploy-pipeline step that applies the set. What the schema currently
holds is `themis/migrate/migrations/` itself, and a service's rights are the GRANTs in the migration that introduced
them; a grant that has not shipped is a migration nobody has written yet.
