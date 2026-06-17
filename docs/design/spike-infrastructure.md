# Design: Spike infrastructure and deployment

**Status:** design landed; walking-skeleton infra implemented (`infra/` + runbooks); Cloud SQL/GCS/audit/sandbox pending
**Parent epic:** [`issues/epic-themis-spike.md`](../../issues/epic-themis-spike.md) (PR #1)
**Related:** [`deployment.md`](deployment.md) decides *how* the infrastructure
is managed (IaC, deploy auth, state, secrets); this doc decides *what* the
Spike needs and the GCP-setup choices around it. The agent runtime/sandbox
choices here track [`agent-runtime.md`](agent-runtime.md).

## Why this exploration

The Spike commits to a Cloud Run web app from day one, backed by Cloud SQL and
GCS, behind IAP, with CPG curators as the user base — real cloud
infrastructure, not a localhost dev environment. The setup must be
reproducible, cost-observable, and secure enough to dog-food with real curators
against synthetic cases. This exploration bundles the GCP-setup concerns that
touch one project: project layout, IAP/SSO, provisioning, secret inventory,
cost, audit, CI/CD, local dev, and the agent execution sandbox.

## Background — already committed by the epic

- **Compute:** Cloud Run (stateless container, scale-to-zero acceptable).
- **Database:** Cloud SQL (Postgres) for structured data.
- **Blob storage:** GCS for artifacts.
- **Auth:** IAP in front of the web app.

The management mechanism (IaC tool, deploy auth, state, secrets) is decided in
[`deployment.md`](deployment.md).

## Decisions

### 1. GCP project structure

Dedicated per-environment projects in the CPG GCP org: **`cpg-themis-dev`**
(project 698160839187) now, **`cpg-themis-prod`** later. One Pulumi stack per
environment, each targeting its own project. Dev/prod separation runs through
every resource — project, access group, state bucket, KMS key, secrets, Cloud
Run services, Artifact Registry, budget. Isolation is by the project boundary;
the project stays inside the org, so it inherits org IAM, central billing,
aggregated audit sinks, and Workspace SSO. A shared project (reusing a data
project) is excluded — it would couple Spike IAM and audit to the data estate.

### 2. Access gate — IAP + SSO

IAP is the hard authentication gate. It grants
`roles/iap.httpsResourceAccessor` to a **per-environment Google Group** (e.g.
`themis-dev-access@populationgenomics.org.au`) — a coarse "may reach the app"
gate, not a role. The group and its membership are managed by PR through CPG's
existing Cloud Identity machinery (the public
[`cpg-infrastructure`](https://github.com/populationgenomics/cpg-infrastructure)
Pulumi: per-environment Google Groups via `gcp.cloudidentity.Group`, see its
README § Group memberships), with rosters held privately, out of this repo; this
repo references only the **group principal**, a single non-PII identifier safe
on the public mirror — never a roster. The group may
contain non-CPG Google accounts (curators are generally external medium-term),
so access is **not** org-domain-gated. Roles, project membership, and
per-report authorization live in the app, not here.

Identity Platform (GCIP) is the path for curators with no Google
identity; deferred — the Spike's initial CPG testers have Google accounts.

Confirm with central infra: external membership in the group, and any
`iam.allowedPolicyMemberDomains` (domain-restricted-sharing) org policy — the
IAP grant is to an in-domain group principal, but external group *membership*
is a separate Workspace setting.

### 3. Audit + data retention

Data Access audit logs are enabled in **dev and prod** (Secret Manager, KMS,
Cloud SQL, GCS), alongside IAP/Cloud Run request logs — the two environments
are identical, so the audit story is exercised early. Audit/access logs route
to a dedicated **regional Cloud Logging bucket** (`australia-southeast1`, not
the `global` `_Default`) with **Log Analytics** enabled, SQL-queryable in place
("who accessed which secret/object"), **90-day retention**. Cost is dominated by
*ingestion* (~$0.50/GiB), not retention (~$0.01/GiB·mo past 30 days); the lever
is which Data Access logs are on — high-volume `DATA_READ` is scoped/excluded
rather than shortening retention.

Reports are append-only: GCS object versioning on, 30-day soft-delete, **no
auto-delete** (kept for the Spike's life); Cloud SQL daily automated backups
retained 30 days, PITR 7 days. A finite erasure/retention policy is a
prod/real-data follow-up — the Spike is synthetic-only.

### 4. Secret inventory + rotation

OIDC/IAM-first (see [`deployment.md`](deployment.md)): Anthropic API via WIF,
Cloud SQL via IAM auth, GCP via the workload SA. A secret is stored only where
there is **no WIF path** — currently just the optional **NCBI E-utilities key**
(raises the ClinVar/literature rate limit; not strictly required).

No DB password (IAM auth), no Anthropic key (WIF), no IAP OAuth client secret
(Google-managed IAP for Cloud Run), no app session key (the app trusts the IAP
JWT per request, stateless).

Self-hosted sandboxes (§8) add **two scoped stored secrets**: the
`ANTHROPIC_ENVIRONMENT_KEY` the sandbox worker uses to claim its work queue, and
the **webhook signing key** (`whsec_…`) that verifies the wake webhook letting
the worker scale to zero — both Secret Manager, scoped to one environment,
rotated on exposure. The control
plane (creating agents/sessions/environments) still authenticates via WIF.
Env-var vault credentials are not supported on self-hosted, so any third-party
key is read host-side by our tool MCP servers from Secret Manager. The
interim cloud sandbox needs no environment key.

Rotation: these no-WIF-path credentials rotate on compromise / per provider
policy (the environment key on exposure).

### 5. Cost observability

Infra budgets reuse CPG's **existing per-project budget infrastructure in
`cpg-infra`** (cpg-themis-dev already carries a ~$500/mo budget), per project,
dev/prod separate — not managed by this repo's Pulumi program. LLM/token cost is an
orthogonal concern, unrelated to GCP infra, and out of scope here. Per-report
**token usage** (input/output/cache, per agent) is captured in the trace from
the session event stream (`span.model_request_end`; see
[`trace-schema.md`](trace-schema.md)) and **rolled up across reports** into a
dev/team dashboard (per agent, per day) — **not** surfaced in the curator report
UI. Cloud Run scale-to-zero
≈ 0 idle; Cloud SQL is the standing-cost floor (no scale-to-zero; smallest dev
tier).

### 6. CI/CD pipeline

Deploy auth and gating per [`deployment.md`](deployment.md): GitHub OIDC → WIF;
**write** deploys only on push to `main`. PRs get a **read-only** preview
identity that runs `pulumi preview` and posts it as a comment (informing the
single PR-approval gate), plus cloud-free validation. No PR job can mutate cloud
state — preview only, never apply.

- PR: lint, type-check, unit tests (no cloud), and a read-only `pulumi preview`
  posted as a comment.
- On squash-merge to `main`: **GitHub Actions builds the image(s)**, pushes to a
  **per-project Artifact Registry** (authenticated via WIF), then `pulumi up`
  points the Cloud Run service at the new image — deploying to
  **cpg-themis-dev**.
- Two images once self-hosted (§8): the **orchestrator/web-app** (Cloud Run)
  and the **sandbox worker** (runs in our infra with controlled egress;
  platform TBD). Agent and environment definitions are version-controlled YAML
  applied via the **`ant` CLI** from CI (control plane).
- Prod (later): a **gated GitHub Environment** (required approval) promotes the
  *same validated image* into prod's Artifact Registry by copy — **no rebuild**
  — and deploys. Full project isolation; prod never reads dev's registry.

### 7. Local dev environment

The agent loop and tools run in the **cloud** (the dev project + Anthropic's
Managed Agents); the tools depend on real data, so emulating them locally adds
no signal. Local dev is therefore two fast paths against dev, not a local clone.

- **Tool / agent iteration:** edit locally, then push to **dev** through a
  dev-only fast path — apply the agent/environment YAML with `ant`, redeploy
  the tool MCP server / sandbox worker to dev — and run a session there,
  inspecting the resulting trace. (Dev-only; the gated `main`→deploy pipeline in
  §6 is unchanged.)
- **UI iteration:** run the frontend and backend **locally**, with the local
  backend connected to **dev Cloud SQL** (Cloud SQL Auth Proxy or the Python
  connector, IAM auth) and **dev GCS**. The frontend talks to the local
  backend; the backend reads real dev data. The proxy is IAM-gated and TLS — no
  public DB exposure, no password, no Docker.
- **Hermetic tests:** schema and logic tests must run with **no cloud** (PRs
  get no cloud access — §6), so they use an **embedded Postgres** (pip-installed
  binary, no daemon) and the storage interface's filesystem blob backend. There
  is no official Cloud SQL emulator — it is managed Postgres, so (unlike Pub/Sub
  or Firestore) the faithful local equivalent is Postgres itself.
- **Auth:** Anthropic via the developer's `ant auth login` short-lived
  credentials; **no IAP locally** (a cloud edge concern), with a configured dev
  user where a flow needs an identity.

**Shared-dev concurrency:** dev is one environment for the whole team, so
concurrent mutation of shared resources — schema, the deployed tool/MCP servers,
agent definitions — is a coordination point, not per-developer isolation. The
only complete cure is full per-developer isolation, which is overkill for the
Spike; so for the Spike (small team) we **coordinate**, and if it chafes we go
to **per-developer projects** (cheap given the Pulumi setup — one stack per
project), not partial isolation. The schema-rollback case specifically:
migrations are **forward-only**, applied to dev only via the merge→deploy
pipeline (§6), so the deployed schema only advances; in-progress schema changes
run against the embedded Postgres harness until merged.

No Docker — everything is a local process making outbound, IAM-gated
connections, so it runs inside the Claude Code sandbox. The constraint is
**Docker-free** (to avoid dev-time sandbox issues), not emulator-free: a service
emulator is fine as long as it runs as a local process, as most GCP emulators
do. The Spike's current deps need none anyway (embedded Postgres, filesystem).

### 8. Agent runtime and execution sandbox

Runtime direction: **Managed Agents** — Anthropic runs the agent loop and emits
the per-session event stream (which feeds the trace); the Themis backend is the
orchestrator/client. The framework choice is ratified in
[`agent-runtime.md`](agent-runtime.md) (currently being revised from the
earlier Agent-SDK draft); recorded here for its infra consequences.

Why Managed Agents for the Spike: it skips building and operating the agent
loop, the execution sandbox, per-session state, and the event stream — so we get
results fast, the Spike's goal. This is **not** meaningful lock-in: the parts we
build — the tool/MCP servers and the orchestrator's data-plane mediation — are
runtime-independent and carry over unchanged, so moving to a custom loop later
swaps only the orchestration layer, not our tools or data plane. The dependency
that does exist is Anthropic's (beta) agent API. Alternatives are weighed in
[`agent-runtime.md`](agent-runtime.md).

Execution sandbox — target is **self-hosted**, for egress control. The
Anthropic-hosted **cloud** sandbox gets the first version running, **gated so
self-hosted lands before any real (non-synthetic) data**; the cloud→self-hosted
move is mostly **additive** (add the worker, MCP tunnels, egress policy), not
rework, because the orchestrator, agent YAML, CI, and data plane are
**sandbox-agnostic**. The near-term target is a **self-hosted sandbox**
(`config: type=self_hosted`): tool execution (bash, the public-endpoint
lookups) runs in a container **in our infra**, driven by a worker that **wakes
on a thin signed webhook** (`session.status_run_started`) and then claims work
by **outbound poll** — so the worker **scales to zero** and work/results flow
outbound (the only inbound is the signed wake notification). The egress controls
(deny-by-default VPC egress, allowlist to the public lookup endpoints / local
mirrors) exist either way; under the Anthropic-hosted cloud sandbox they are
configured on Anthropic's side, and **self-hosting moves that configuration to
our boundary** — egress for the agent's own bash/generated code is then governed
where we set the policy.

- **Tools via MCP tunnels:** the lookup/tool MCP servers run in our network and
  are reached by the managed loop through **MCP tunnels** — no public endpoint
  exposed.
- **Sandbox container:** under self-hosted we build and harden it — minimal
  base, curated **binary allowlist** (no egress/network tools, no runtime
  package manager), tracked manifest under CODEOWNERS + security review. Under
  the interim cloud sandbox this is Anthropic's.
- **Isolation:** the sandbox holds no GCP credentials and no metadata-server
  access; egress is deny-by-default at our boundary. The orchestrator (which
  holds the GCP/Anthropic identity) mediates access to private data
  (Cloud SQL/GCS) via custom tools / our MCP servers — the sandbox never
  touches the data store directly.
- **Config + code execution:** the agent config — system prompt, tool list, MCP
  server URLs, model id — lives on the Agent object and is pushed at **deploy
  time via `ant`** (control plane, §6), not bootstrapped per-poll; MCP
  credentials attach per session via vaults. The concrete per-agent model id is
  secret-class confidential — see *Confidential config* in
  [`deployment.md`](deployment.md). Generated analysis code is not squeezed
  through MCP — it runs via the toolset's `bash`/file tools **in the self-hosted
  worker**, under our egress policy.

See [`agent-runtime.md`](agent-runtime.md) for the loop topology and the
runtime-side of the self-hosted-sandbox / MCP-tunnel design.

## Constraints

- Cloud Run target — stateless container, scale-to-zero acceptable.
- IAP-fronted; no public-internet access to the app.
- Reproducible — bring up a fresh environment from scratch without tribal
  knowledge.
- No plaintext secrets in the repo, ever; secrets never logged. KMS-gated
  ciphertext under the secrets provider is fine — see
  [`deployment.md`](deployment.md).
- Cost-bounded — per-project budgets (§5) flag unusual spend.

## Deliverable

- The Pulumi program (per stack: project services, Cloud Run, Cloud SQL with
  IAM auth, GCS, IAP + group binding, Artifact Registry, audit log bucket/sink,
  Secret Manager secrets).
- The CI/CD workflow (build → AR → `pulumi up` on `main`; gated prod promotion;
  agent/environment YAML applied via `ant`).
- The Managed Agents wiring: the orchestrator/client and, in the self-hosted
  phase, the sandbox worker image + binary-allowlist manifest and egress policy
  (§8).
- The local dev setup: the tool/agent fast-path to dev, Cloud SQL Auth Proxy
  config for local UI against dev, and the embedded-Postgres test harness (§7).
- A runbook for bringing up a fresh environment (absorbs the
  [`deployment.md`](deployment.md) bootstrap: state bucket, KMS key, WIF pool).

## Out of scope

- Application code (web app, agents) — the other explorations.
- Talos / Metamist / seqr integration — Spike uses public endpoints only.
- Multi-tenant or external-customer concerns.
- Per-report authorization, roles, project membership — app-side, not infra.
