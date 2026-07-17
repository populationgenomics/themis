# Plan: the SQL migration runner (`db/`)

## Context

The Cloud SQL Postgres instance exists ([`../design/deployment.md`](../design/deployment.md);
`infra/themis_infra/sql.py`, #88): IAM database auth, connector-only (it refuses direct connections). It has an
application database and no schema — no tables, no runner. `sql.py` is explicit that "the runtime SAs' read/write split
is enforced by table GRANTs in the migrations, not here", and #93 confirms table DDL + grants are "the migration's job",
an unbuilt follow-up. Two consumers are blocked on it: the auth service's Cloud SQL resolver adapter (it reads a
session-token table; `services/auth` ships only a fixture backend until this lands) and litcache's crosswalk.

Prior art: the superseded MCP branch (`feat/managed-agents-wiring`, #76) already had a lean, unit-tested runner —
`mcp/store/themis_store/migrate.py`: forward-only `NNNN_name.sql` files, contiguous versions, a `schema_migrations`
ledger, `${VAR}` substitution for the per-SA GRANT logins, explicitly "no ORM, no Alembic". #76 applied it from a
deploy-workflow step. This plan ports that design into the post-#83 world, where it is shared (no single service owns
it) rather than living under the store.

## Decisions

Settled (interview):

- **One shared migration set, one `public` schema** — a single contiguous `NNNN` sequence for the whole database, not
  per-domain sets.
- **Hand-rolled runner**, porting #76's `migrate.py` — no ORM, no Alembic, no third-party migration CLI.
- **Runs from the deploy pipeline** (a CI step after `pulumi up`), connecting as a schema-owning **migrator DB role**.
- **Hand-written SQL** is the source of truth for the schema (TypeSpec stays wire/at-rest *model* authoring, not DDL).
- **First migration = the `session_context` table, standalone** (`token_hash`, `project_id`, `analysis_id`; no foreign
  keys) — enough to unblock auth.
- **Lives in a top-level `db/`.**

## Layout — `db/`

```
db/
  migrations/            NNNN_name.sql, forward-only, contiguous from 0001, all in `public`
  themis_migrate/        the runner package (import name themis_migrate)
    migrate.py           discover / render / split_statements / run + the Ledger protocol
    cloudsql.py          CloudSqlLedger — the connector + pg8000 apply, under a session lock
    config.py            the Cloud SQL + substitution inputs, read from the environment
    __main__.py          entrypoint: load config, discover, run
  tests/                 unit tests over InMemoryLedger
```

[`../repo-structure.md`](../repo-structure.md) gains a `db/` row; the runner is application code (SQL + Python), so it
lives here, not in `infra/` (Pulumi-only).

## The runner

Port `migrate.py` in shape (it is already the right design):

- `Migration(version, name, sql)` discovered from `db/migrations/*.sql`; `discover` validates the `NNNN_name.sql` form
  and a contiguous 1-based sequence (a gap or duplicate is an error).
- `render(sql, substitutions)` substitutes `${VAR}` placeholders — the GRANT logins (below).
- `split_statements(sql)` splits on top-level `;` (pg8000 executes one statement per call), ignoring `;` inside
  single-quoted strings and `--` comments.
- `run(migrations, ledger, substitutions)` applies pending migrations in version order, **forward-only**: a pending
  version at or below the highest applied one is rejected. Re-runs with nothing pending are a no-op.
- `Ledger` protocol — `applied_versions()` + `record(migration, sql)` (apply the SQL and mark the version applied,
  **atomically**). `InMemoryLedger` for unit tests; `CloudSqlLedger` for the real apply.

Additions for the real ledger:

- **`schema_migrations`** ledger table (`version` PK, `name`, `applied_at timestamptz`), created idempotently on the
  first run.
- **Transactional per migration** — each migration's statements plus its `schema_migrations` insert run in one
  transaction, so a failed migration is never half-recorded.
- **Concurrency lock** — `apply_migrations` holds one connection for the whole run and takes a session-level
  `pg_advisory_lock`, so two concurrent runs (overlapping deploys) serialize: one applies, the other finds nothing
  pending. Released when the connection closes.
- **Forward-only, no down-migrations** — matches the additive-only schema-evolution stance
  ([`../design/proto.md`](../design/proto.md), "Schema evolution"). A correction is a new forward migration.

The pure runner is unit-tested against `InMemoryLedger`; `CloudSqlLedger` is verified against a live instance at deploy
(it is not exercised offline).

## Execution — a deploy-pipeline step

After `pulumi up`, the deploy workflow runs `uv run python -m themis_migrate` before rolling the services that depend on
the new schema. It connects through the **Cloud SQL connector** (IAM database auth) as the **migrator DB role**,
discovers `db/migrations/`, renders the grant logins, and applies the pending migrations idempotently — safe to run on
every deploy. Config comes from the environment (`THEMIS_SQL_CONNECTION_NAME` / `THEMIS_SQL_DATABASE` /
`THEMIS_SQL_IAM_USER`, and `THEMIS_MIGRATE_SUBSTITUTIONS`).

A Cloud Run Job was considered, so that CI would only *trigger* migrations rather than hold a DB login. It buys nothing:
the deploy identity already provisions Cloud SQL and IAM, so a compromised runner can grant itself a DB login or rewrite
the Job regardless. The Job's orchestration cost is not worth a non-boundary, so the simpler, proven CI step wins.

## Identity and the ownership bootstrap

The **migrator DB role** owns the `public` schema — it `CREATE`s the tables and `schema_migrations` and issues the
GRANTs — while the service DB roles only ever get table-level rights. (A DB-level least-privilege split, not a defence
against a compromised deploy identity, which is omnipotent regardless.) Whether the migrator role is a dedicated
migrator SA or the deploy SA itself is a DB-hygiene choice, deferred to the deploy PR.

The wrinkle: a freshly-created Cloud SQL IAM user has a login and nothing else — it cannot `CREATE` until something
grants it (`infra/themis_infra/sql.py`'s `grant_iam_service_account` attaches only login + connect/authenticate). So a
one-time **bootstrap** must grant the migrator role ownership of the `public` schema before its first run:

- **(a, recommended)** A one-time Pulumi step, as the instance's built-in admin, grants the migrator ownership
  (`GRANT CREATE ON SCHEMA public` / `ALTER SCHEMA public OWNER TO`).
- **(b, rejected)** Run migrations as the built-in `cloudsqlsuperuser` — over-privileged and not IAM-authenticated.

## Grants (the read/write split)

Grants are ordinary forward-only migrations, exactly as #76's `0002_grants.sql`: `GRANT` statements with `${VAR}`
placeholders for the service SA **DB-user logins** (the SA email minus the `.gserviceaccount.com` suffix, matching
`sql.py`). The deploy step passes the substitution map (login names, from the SA resources) as
`THEMIS_MIGRATE_SUBSTITUTIONS`. Adding a service means adding a new `NNNN_grants_<service>.sql` migration — never
editing an applied one.

First slice, on `session_context` (`0002_grants.sql`): the **auth SA** gets `SELECT`. The BFF write grant lands with the
token-minting write-side, when the BFF SA is attached as a DB user.

## First migration — the `session_context` table

`0001_session_context.sql` creates the table auth reads — named for the `SessionContext` it stores (keyed by token
hash), post-#83 vocabulary, not `mcp_session_grant`:

```sql
CREATE TABLE session_context (
    token_hash  text PRIMARY KEY,       -- SHA-256 of the bearer, never the plaintext
    project_id  text NOT NULL,          -- opaque text; FK to project(id) when that table lands
    analysis_id text NOT NULL           -- opaque text; FK to analysis(id) when that table lands
);
```

Minimal: exactly what `resolve` reads. `project_id` / `analysis_id` are opaque text with **no foreign keys yet** — the
`project` and `analysis` tables are BFF-owned session-plane schema. Token lifecycle (`expires_at` / `revoked_at`, and
their enforcement) and audit (`created_by`) land with the token-minting write-side, as forward migrations — they need a
writer to be meaningful. `0002_grants.sql` follows with the auth `SELECT` grant.

## Downstream (separate PRs, gated on this)

- **auth's Cloud SQL adapter** — a `cloudsql` backend (`THEMIS_BACKEND=cloudsql`) in `services/auth` reads
  `token_hash → (project_id, analysis_id)` through the same connector/driver, replacing the fixture in the deploy.
- **The deploy wiring** in `infra/` + the deploy workflow: the migrator DB role and its bootstrap, and the step that
  runs the runner with the Cloud SQL env + grant-login substitutions.

## Testing

- **Unit** — `discover` / `render` / `split_statements` / `run` against `InMemoryLedger` (port #76's `test_migrate.py`):
  ordering, contiguity errors, forward-only violations, placeholder substitution, statement splitting; plus `config` env
  parsing.
- **Live** — the runner's first real apply is verified at deploy against the instance.

## Open decisions (resolve in review)

- **Migrator DB role** — a dedicated migrator SA vs the deploy SA, plus the ownership bootstrap (approach (a) above).
- **litcache adoption** — it uses the same runner; whether its tables sit in `public` or a `litcache` schema is deferred
  to when litcache migrates.

## Out of scope

- The `analysis` / `project` session-plane schema (BFF-owned).
- The working document — GCS-backed in #83 (immutable version-per-blob), not a relational table; the old
  `working_document*` / `source_snapshot` / `session_projection` tables from #76 do not come across.
- The auth Cloud SQL adapter and the deploy wiring (their own PRs, above).
