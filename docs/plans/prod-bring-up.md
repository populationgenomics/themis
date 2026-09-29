# Plan: bringing up the prod environment

**Related:** [`../design/deployment.md`](../design/deployment.md) (how a stack is deployed, who may deploy it, and where
its state and secrets live); [`../design/spike-infrastructure.md`](../design/spike-infrastructure.md) (the per-project
environment layout, audit and retention); [`../runbooks/fresh-environment.md`](../runbooks/fresh-environment.md) (the
bring-up procedure this plan corrects); [`../runbooks/claude-api-wif.md`](../runbooks/claude-api-wif.md) (the
Anthropic-side identities each environment needs); [`../design/security.md`](../design/security.md) (the controls real
data depends on).

## Overview

Themis has one environment, `dev`, which doubles as the demo environment and runs synthetic cases. This plan brings up
`prod` in its own GCP project, `cpg-themis-prod`, holding CaRDinal research data from launch. That goes beyond the Spike
as [`../PRODUCT.md`](../PRODUCT.md) §12 defines it ("public evidence sources only"), and the product doc changes with
this plan.

The fresh-environment runbook's premise is that prod is the same Pulumi program with a second `Pulumi.prod.yaml` and no
program change. An audit of the program, the workflows, `bootstrap.sh` and the operator tools found that this holds for
resource names and little else. Every name is scoped to its project, so the two stacks cannot collide, and no resource
points at another project. What does not hold:

- The documented first bring-up fails on an empty project. It was written when the program had one service and has not
  been exercised against the program as it is now; every later service was first created on dev by a CI deploy with its
  real image.
- Everything that drives a stack from outside the program is dev-only: the deploy and preview workflows, the bootstrap
  script's default project, the test that runs the program under mocks, and the default project of every operator tool.
- The production-grade settings (database tier and availability, minimum and maximum instances) are constants, so prod
  cannot differ from dev without a program change.
- Real data puts on the critical path the controls that the design docs defer until real data, and some of them are not
  built.

The decisions this plan takes:

1. Prod deploys on every merge to `main`, after that commit's tests pass, through a GitHub Environment. The deploy
   credential is bound to the Environment rather than to a branch, and images are rebuilt from the `main` commit rather
   than promoted from dev.
1. Every PR gets a preview against prod as well as dev, as each stack's separate read-only preview account. That
   account's project-wide viewer role narrows to the few lookups the program makes; its ability to decrypt the stack's
   secrets is accepted on the trusted-membership condition dev already relies on.
1. The group of operators who may impersonate `themis-clu` (the account used to drive a deployed service by hand) is
   shared by both environments. In prod, clu cannot act as a live user session.
1. The first bring-up becomes a script that creates the registry first and deploys real images, replacing the
   placeholder-image procedure.
1. Prod gets its own Anthropic workspace, as [`claude-api-wif.md`](../runbooks/claude-api-wif.md) already specifies, and
   every Anthropic-side id is per-stack config.

The long pole is external: the project and its org policy, two rounds of Anthropic org-admin registrations, and DNS.
Those requests start first, in parallel with the repo changes.

## Background

### Real data at launch

CaRDinal is CPG-managed, consented research data ([`../PRODUCT.md`](../PRODUCT.md) §3). Themis stays research-only;
nothing here makes it a clinical tool. It is, though, the first non-synthetic data any environment carries, and several
design decisions were accepted on the explicit condition that data is synthetic:

- The PR preview account can decrypt the stack's secrets, accepted because "the read-only token is dev-scoped (synthetic
  data)" and, at the time, the stack held no secrets ([`deployment.md`](../design/deployment.md) § Deploy
  authentication).
- The residual exfiltration channel through the agent's web tools is accepted and monitored, with the acceptance to be
  revisited at the non-synthetic-data gate ([`self-hosted-sandbox.md`](self-hosted-sandbox.md) §12).
- Reports are kept with no auto-delete, and "a finite erasure/retention policy is a prod/real-data follow-up"
  ([`spike-infrastructure.md`](../design/spike-infrastructure.md) §3).
- Destructive migrations are allowed while the environments they run in "hold no data worth keeping"
  ([`migrations.md`](../design/migrations.md)).
- The log of calls to upstream evidence services holds case identifiers and is readable by project log viewers. That is
  acceptable while those viewers are the people who can read the case anyway; "the entry would need its own bucket in an
  environment where the two came apart" ([`security.md`](../design/security.md)).

Prod breaks each of these conditions, so each is revisited below.

### Why dev cannot serve as pre-prod

Dev is deployed ad hoc from unreviewed branches, including their infrastructure program and their migrations. Its schema
lineage can therefore diverge from `main`'s, and an image that ran on dev was usually built from a branch commit that
never reached `main`. A green dev deploy says little about what a `main` deploy to prod will do. The plan leaves dev as
it is and gets its assurance from checks that run against `main` itself.

### The two tiers of a bring-up

A fresh environment has two layers. `bootstrap.sh` creates what Pulumi itself depends on: the state bucket, the KMS key
that encrypts stack secrets, the GitHub workload identity pool, and the deploy and preview accounts. `pulumi up` creates
everything else. Some config values name things only the first `up` produces (the ids of service accounts that Anthropic
federation rules pin), so a first bring-up runs against placeholders for those, and a second `up` applies the real
values.

### What the bring-up builds on

- The deploy workflow applies the agent declaration (which agent, skills and model a session runs) after `pulumi up`,
  signed in to Anthropic as the deploy account. The declaration and its skill ids are tracked in the repo.
- IAP guards the web app on its Cloud Run service. No IAP setting depends on an id that only the first `up` produces.
- Each stack sets the ratio at which request traces are sampled. The auth service keeps a pool of database connections
  and runs at most three instances.
- The migrate runner compares its ledger with the committed migrations before it applies anything, by content hash where
  the ledger recorded one. The deploy runs the same comparison read-only before it builds images or runs `pulumi up`, so
  a ledger row that disagrees with the committed file of the same version stops the deploy.

One dependency is still open. #681 adds each environment's operational alerts channel (`themis:alertsSlackChannel`),
with a first alert for schema drift in literature ingest. The Alerting section and prod's Slack app assume it merges
first.

## Design

### Deploy path

Prod deploys on every push to `main`, and can be redeployed by dispatching the workflow on `main`. The deploy job names
a GitHub Environment, `prod`, whose deployment-branch policy admits `main` alone, so a job naming it from any other
branch never starts.

Each deploy account's workload identity binding matches the OIDC token's `environment` claim, not its branch. GitHub
issues a token carrying `environment: prod` only to a job that passed the Environment's protection rules, so the
Environment's configuration becomes the credential boundary. This closes a gap the branch binding leaves open: today any
workflow that runs on `main` with permission to request a token can take the deploy account, and three workflows
qualify, one of which runs an LLM agent. Dev's deploy job runs in a `dev` Environment that admits `main` and
`deployed/dev`, so the gap closes on both stacks. Each pool needs one more attribute mapping for the claim.

The deploy job waits for the tests on the commit it deploys. `main`'s required checks are not strict, so two
individually green PRs can merge into a broken `main`. The ledger preflight catches one form of this before `pulumi up`:
two PRs that each add migration `0013` leave `main` with a duplicate version, which the check refuses. A conflict in the
code itself has no such check, and an ungated deploy would roll the new images before the failing tests were seen. The
deploy workflow calls the lint, test, web and schema workflows as reusable workflows and makes the deploy job depend on
them, which guarantees they ran on the same commit. The cost is running those checks twice on each merge, once
standalone and once inside the deploy, unless their standalone push trigger is dropped.

The review of an infrastructure change happens on its PR, which carries a preview against prod's state (below). The
deploy job also runs `pulumi preview` before `up`, as a record in the deploy log. Nobody reads that one before the `up`
applies, and prod's state can move between a PR's review and its deploy, when another PR merges first. Putting a person
between the deploy's preview and its `up` is the second-approver question under open questions.

Images are rebuilt from the `main` commit into prod's own registry. Promoting dev's image
([`spike-infrastructure.md`](../design/spike-infrastructure.md) §6) was the alternative and is rejected below. A rebuild
is not byte-identical to an earlier build of the same commit: base images are pinned by tag, and the sandbox worker's
Dockerfile runs unpinned `apt-get` in two stages. Pinning base images by digest narrows that, and is part of the work.

The deploy workflow becomes one reusable workflow parameterised by environment, called for dev (its existing triggers)
and for prod. Its concurrency group, stack, state bucket, identity provider, account and image prefix come from the
environment rather than from literals.

`deployment.md` and `spike-infrastructure.md` §6 disagree about prod's trigger and promotion. Both are rewritten to this
decision in the design-doc PR that precedes the implementation.

### PR previews against both stacks

Every PR gets a `pulumi preview` against dev and against prod, posted as comments. Each runs as its stack's preview
account, which `bootstrap.sh` creates alongside the deploy account and binds only to `pull_request` tokens. It reads
state and never writes. The preview workflow is parameterised by stack like the deploy workflow, with a concurrency
group per PR and stack.

The design question is what that account can reach. It holds three grants:

- Read on the state bucket, which it needs.
- Decrypt on the stack's KMS key, which it also needs: the gcpkms secrets manager decrypts the stack's data key on every
  operation, preview included, so a preview without decrypt fails rather than showing a partial plan.
- `roles/viewer` on the project, which is far more than it needs. Preview does not refresh, so beyond reading state it
  only makes the program's four lookups (a service's live image, a job's live image, the KMS key ring and the project
  number). `roles/viewer` also reads the project's logs, among them the upstream-call log and its case identifiers. In
  both stacks it is replaced by a custom role holding the permissions those four lookups need.

What remains is decrypt. A PR runs its own copy of the workflow file and its own lockfile, so any code running in its
preview job can decrypt the stack's secrets. That code could come from a compromised collaborator account or from a
malicious release of a dependency the PR bumps. The secrets it would reach are the Anthropic environment key (which can
claim sandbox work for the whole environment, and so act inside sessions over CaRDinal data), the webhook signing key
and the Slack token. The deploy account is far more powerful, but only admins can make a job run as it. The preview
account is the one prod credential that every collaborator's PR can take, and collaborators with write outnumber admins
several times over.

This is accepted in prod on the condition dev already relies on: the repo is private, forking is disabled, and every
collaborator is trusted. The dependency release-age gate narrows the dependency path without closing it. If the
condition stops holding (collaborators with no reason to reach CaRDinal-derived access, say, or external contributors),
the remedy is a separate secrets stack; see Alternatives considered. `deployment.md` states the acceptance on that
condition in place of its current one ("tolerable while the dev stack stores no secrets"), which dev no longer meets.

Previews run on non-draft PRs only and are not a required check. So a cloud-free check still guards prod's config: the
test that runs the program under Pulumi's mocks against a stack's config (the same test that enforces the grants rule).
Today it runs against dev's config only, with every opt-in switched on. It becomes one capture per stack file, each in
its own process because the mocks are process-global, so a key missing from any stack fails the PR. A separate check
reads prod's stack file directly and fails if an opt-in that prod must not hold is on: the PR-screenshot bucket, clu's
session-bearer derivation, and the CI telemetry account. Both checks cover whichever stack files exist, so they start
covering prod when its file is committed.

The CI telemetry account (the identity the Claude Code workflows use to write their token usage to Cloud Monitoring)
accepts a token from any run of the repository. All three workflows write to dev. In prod the account would be unused,
and any PR could use it to write prod metrics, including the spend metrics prod's alerts read. It becomes a per-stack
switch, off in prod.

### `themis-clu` in prod

Each project has its own `themis-clu` service account, and one Google group decides who may impersonate it. Its database
login belongs to the migrator role, which owns every table. Prod's clu account is impersonable by the same group as
dev's.

A separate prod group would guard against one failure: someone added to the group for dev work would gain full reach
over CaRDinal data as well. The group is a handful of people who administer both projects, its roster changes by PR in
`cpg-infrastructure-private`, and that review is where such an addition is caught. A project administrator can reach the
database through their project roles regardless, so a second group would double every roster change without narrowing
the reach of anyone currently in it. If the set of prod operators ever becomes narrower than dev's, the group splits
then.

The accidental risk remains: with the same people able to become clu in both projects, a command meant for dev reaches
prod if it names the wrong project. The operator tools stop defaulting their project for that reason (see Tooling).

In prod, clu cannot derive the bearer token of a live user session, which in dev lets an operator act as any session.
The docstring of [`themis/clients/auth/rules.py`](../../themis/clients/auth/rules.py) says clu access is "granted
just-in-time in prod", which describes no mechanism that exists, and is corrected.

### Production settings per stack

The dev-grade settings become per-stack config holding dev's current values, so the program stays one program and prod's
values are reviewable in its stack file:

- Cloud SQL tier and availability. The current tier is shared-core with no SLA and cannot run highly available. Prod
  wants a dedicated-core tier, regional availability and a maintenance window. Backups and deletion protection are
  already right.
- Minimum instances for the web app and the auth service, which every call passes through. At zero, the first request
  after idle waits for a cold start.
- Maximum instances on the services that hold database connections, sized against the tier's connection limit. The auth
  service is capped at three by #664, as a constant; it becomes per-stack config here.
- The trace sample ratio (`themis:traceSampleRatio`, #661). Prod starts at 1.0, the same as dev, while its traffic is
  small.

Three settings change in the program for both stacks, because dev loses nothing by having them:

- An SSL policy on the HTTPS proxy with a TLS 1.2 minimum. Without one the proxy gets GCP's default, which admits TLS
  1.0.
- Deletion protection (`protect`) on the session-token KMS key. Every live session's bearer is signed with its first
  version, so replacing the key strands every session.
- Request logging on the load balancer's backend.

Cloud Armor is left out. IAP refuses unauthenticated traffic at the web app's Cloud Run service before it reaches the
app, and the one unauthenticated public surface, the sandbox dispatcher, is not behind the load balancer at all (it is a
Cloud Run URL that checks the webhook's signature), so a policy on the load balancer would not cover it.

### Audit logging

[`spike-infrastructure.md`](../design/spike-infrastructure.md) §3 specifies Data Access audit logs for Secret Manager,
KMS, Cloud SQL and GCS, routed to a regional log bucket with 90-day retention. None of it is in the program, and dev's
project IAM policy carries no audit config. The org may enable some of it above the project, which needs checking; the
design wants a regional, project-owned bucket regardless. It lands as a program module, in both stacks, before prod
carries data.

The upstream-call log is the second question. In prod, the project's log viewers (including org-inherited ones and the
deploy account) are a wider set than the members of any one Project, which is the case `security.md` names. The proposal
is to route those entries to a log bucket of their own, readable by operators only. The decision belongs in
`security.md`, and the bucket in the audit module.

### Alerting

Today's only alerting is the spend monitor (the cost-monitoring alert policies and dashboard). Prod adds Cloud
Monitoring policies for:

- Failed executions of the sandbox job and the weekly gene-disease refresh. The spend monitor's daily report job already
  alerts this way (the job's completed-execution count with a failed result), and the pattern generalises directly. The
  cost exporter needs nothing new: its heartbeat alert already covers every way it can fail.
- Cloud Run 5xx rate and latency per service.
- Cloud SQL CPU, disk and connection saturation.
- Papers stranded by the full-text conversion queue. Cloud Tasks has no dead-letter destination, so a task that exhausts
  its retries disappears; the reconcile sweep in [`evidence-fulltext.md`](../design/evidence-fulltext.md) is what finds
  such papers, and its cadence, still unchosen, bounds how long one stays stranded. The alert watches the sweep's
  findings.
- Certificate expiry on the load balancer.

A failed deploy is a GitHub Actions event rather than a Cloud Monitoring signal, so the deploy workflow gets a
notification step of its own.

The sandbox queue-depth alert (the designated detector for Anthropic auto-disabling the webhook) is specified in
[`self-hosted-sandbox.md`](self-hosted-sandbox.md) against an org API key, which conflicts with the spend monitor's rule
of holding no admin-capable identity. It needs a design of its own; until it exists, a disabled webhook shows up as
sessions that never start.

The deploy account already holds the monitoring roles, so the policies are program-managed. Operational alerts post to
the environment's own alerts channel (`themis:alertsSlackChannel`, from #681), separate from the spend channel, through
that environment's Slack app.

### The first bring-up

The documented procedure passes one public placeholder image, for the web service. The program reads an image override
for every service and job and, for any override left unset, looks up the image the service is running now, which fails
when the service does not exist. Overriding them all does not rescue it: the four gRPC services probe the gRPC health
service at startup, the placeholder serves plain HTTP, and they would never become ready. The CI deploy cannot do the
first bring-up either, because it pushes images before `pulumi up` and the registry is created by `pulumi up`. And the
first `up` has to run as an operator with Owner, because the deploy account cannot create the program's custom
sandbox-job role.

The evidence service adds an ordering constraint. It loads the gene-disease dumps at startup and fails its startup probe
if they are missing, and the job that writes them runs weekly. A full `up` on an empty resources bucket therefore fails
at the evidence service.

The replacement is a script, run once per environment by an Owner:

1. With every image override set to a placeholder, a targeted `up` of the Artifact Registry repository. Pulumi still
   evaluates the whole program under a targeted `up`, which is why every override has to be set even though no service
   is created.
1. Build and push every image from a `main` commit into that registry.
1. A targeted `up` of the resources bucket and the gene-disease refresh job (with the account and grants the job runs
   under), using the job's real image, then one execution of the job.
1. A full `up` with the real images.
1. The migrations.

Scripting it matters because steps 1 and 3 are precisely the kind of detail that runbook prose gets wrong. The runbook's
placeholder table also grows to cover what it misses: the webhook signing key (the webhook is registered against the
dispatcher's URL, which only the first `up` produces) and the convert worker's federation ids. For prod the convert
worker is handled like the cost exporter, with placeholders first and the rule registered afterwards. Dev's worker
account was created by hand and imported because its rule had to be registered ahead of the program; prod has no such
constraint.

`bootstrap.sh` changes for prod too. Its project argument becomes required rather than defaulting to dev. Its error
suppression narrows to "already exists" and "not found", so a real failure, such as a failed default-VPC delete, is
reported. The preview account's project role becomes the custom lookup role, and each deploy account's binding takes the
`environment` claim.

### Anthropic side

Prod gets its own workspace, `cpg-themis-prod`, as [`claude-api-wif.md`](../runbooks/claude-api-wif.md) specifies.
Sharing dev's would also make each environment's cost exporter count the other's spend, because the exporter is scoped
by its token's workspace. The workspace needs:

- Four Anthropic service accounts, each with a federation rule pinned to one GCP service account: the web app, the cost
  exporter, the convert worker, and the deploy account. Only an org admin can create them.
- A self-hosted environment and its key, the webhook and its signing key, the agent, and its skills. A workspace member
  can create these. The runbook for creating an environment is marked superseded and has to be rewritten first.
- A spend limit, which bounds what a misbehaving prod agent can cost.
- Data-retention and inference-geography settings chosen for CaRDinal-derived content rather than inherited from the org
  default. The Claude API reference describes both as per-workspace settings.

The web and cost-exporter rules pin service accounts that only the first `up` creates. The deploy account exists after
`bootstrap.sh`, and the convert worker's rule can wait. That makes two org-admin rounds: the workspace and the deploy
account's rule before the first `up`, the other three after.

The agent declaration names its skill ids in tracked YAML. An API token sees only the resources of the workspace it is
bound to, so a skill created in dev's workspace is presumably invisible from prod's. The reference does not say so for
skills specifically, and one lookup of dev's skill id with a prod-workspace token settles it. If it holds, one file
cannot serve both environments, and the skill ids move to per-stack input to the agent tooling, as the agent id already
is.

### Data

Some of what the running system reads is created by neither `bootstrap.sh` nor `pulumi up`:

- The literature corpus. Dev's full-text store was built from a seed dump that exists only in dev's bucket and is marked
  for deletion after ingestion. Prod either copies dev's built paper store and rebuilds the crosswalk from its
  manifests, which also carries the GeneReviews chapters, or re-runs the ingest. The copy is cheaper and avoids paying
  for transcription twice. Either way, the seed stays until prod's corpus exists.
- The reference mirror, loaded by the resource mirror tool into prod's bucket. No running service reads it yet, so it
  does not block launch.
- The Project and membership rows. A fresh database admits nobody until they are seeded, which is done by hand as clu;
  the rosters are personal data and stay out of the repo.

Variant annotation reads live public APIs, so there is nothing to copy for it. How CaRDinal data itself reaches prod is
not covered by any design doc; see open questions.

### Tooling

- Every operator tool that takes a project (the clu tools, the resource mirror, the litcache tools) requires it rather
  than defaulting to `cpg-themis-dev`. With the same people able to become clu in both projects, a silent default is the
  likeliest way to run a dev command against prod.
- The per-stack config checks described above.
- The destructive-migration allowance in `migrations.md` ends. Prod will hold data worth keeping (and dev's curation
  rows are already handled as such, by backfill rather than a destructive change), so a column is dropped by expand and
  contract from now on.

## Sequence

The phases overlap: phase 1 starts now and runs alongside phase 2, and phase 3 waits on both. Phase 4 stands between a
running prod and the first CaRDinal data.

### Phase 1: external requests

1. Confirm `cpg-themis-prod` exists with billing, request its budget in `cpg-infra`, and check its org policy. Dev's
   project sits directly under the org; a stricter folder for prod would block public access to the dispatcher, the
   Cloud SQL public IP (the instance is reached only through the connector, but the IP must exist), or the Dataflow
   workers' external IPs.
1. Choose prod's hostname and tell the IT team an A record will follow, so the request is ready when the IP exists.
1. Create the prod IAP access group through `cpg-infrastructure-private`, and check that the OAuth consent screen's user
   type admits the external curators who will use prod.
1. Anthropic org-admin round 1: the prod workspace, its spend limit, retention and geography settings, and the deploy
   account's service account and rule.
1. A Slack app of prod's own, `Themis prod`, so that a post shows which environment sent it and dev's token cannot post
   into prod's channels. It is invited to prod's spend channel (`themis:slackChannel`) and prod's operational alerts
   channel (`themis:alertsSlackChannel`, from #681), and its bot token is prod's `themis:slackBotToken`, which the first
   `up` already requires.
1. The skill-scoping lookup described under Anthropic side.
1. Keep the literature seed: agree that dev's seed dump is not deleted until prod's corpus exists.

### Phase 2: changes on `main`

Items without a stated dependency can run in parallel.

1. The design-doc PR, which needs a second maintainer under the review policy:
   - `PRODUCT.md` §12 widened to real research data in prod.
   - `deployment.md` and `spike-infrastructure.md` rewritten to the deploy path above, and `deployment.md`'s preview
     acceptance restated on the trusted-membership condition.
   - The `migrations.md` rule change, the `security.md` upstream-log decision, and the retention and erasure policy.
   - Corrections to stale claims: the "no program change" premise (in `infra/Pulumi.dev.yaml`, `infra/README.md` and the
     fresh-environment runbook); "no secrets are stored yet" in `infra/Pulumi.dev.yaml`; the Secret Manager inventory in
     `deployment.md`, which lists one secret where there are four; `spike-infrastructure.md` §3 describing audit logging
     as enabled and the report bucket as versioned with a 30-day soft delete; the plaintext model id in the
     sandbox-probe agent YAML against `deployment.md`'s confidential-config rule; and `infra/README.md`'s list of keys
     not to copy between stacks, which misses the Anthropic workspace, service-account, worker service-account and agent
     ids and does not warn against reusing dev's environment and webhook keys.
1. `bootstrap.sh`: required project, narrowed error suppression, the preview account's custom lookup role in place of
   `roles/viewer` (applied to dev as well), and the environment-claim binding for both stacks, with dev's deploy job in
   a `dev` Environment.
1. The program: the per-stack production settings, the CI telemetry switch, the settings changed for both stacks, and
   the audit module with the upstream-log bucket.
1. Alerting, and the deploy workflow's failure notification.
1. The per-stack config checks, and required projects in the operator tools.
1. Base images pinned by digest, and the `rules.py` docstring corrected.
1. The reusable deploy workflow, with the prod job gated on tests and running `preview` before `up`, and the preview
   workflow parameterised by stack. Depends on 2.
1. The bring-up script and the corrected fresh-environment runbook. Depends on 3.
1. The rewritten runbook for creating a self-hosted environment, and per-stack skill ids in the agent tooling if the
   lookup in phase 1 calls for them.

### Phase 3: bring-up

1. `bootstrap.sh` for prod, then the operators' KMS grants.
1. Write and commit prod's stack file, initialised with the gcpkms secrets provider; the per-stack checks start covering
   it on that PR.
1. In the prod workspace, create the self-hosted environment and its key, upload the skills from source, and create the
   agent. The webhook waits for the dispatcher's URL.
1. Run the bring-up script. Then send a test notification from each Slack notification channel it created and check that
   it arrives in Slack: a post from a bot missing from the channel is refused, and Cloud Monitoring reports nothing.
1. Hand the load balancer's reserved IP to IT for the A record; the managed certificate becomes active once the record
   resolves.
1. Anthropic org-admin round 2 against the new service-account ids, and register the webhook. Set the rule ids and the
   signing key, and run `up` again.
1. Load the literature corpus and the reference mirror, and seed the Projects and their members.
1. Create the `prod` GitHub Environment and hand deploys to it.
1. Smoke-test an Analysis end to end, and fire one alert of each kind to confirm it arrives.

### Phase 4: before real data

Prod carries no CaRDinal data until each of these is settled, with the decision recorded in the doc that owns it:

1. The data path by which CaRDinal data reaches prod.
1. Anthropic's retention of session content for CaRDinal-derived Analyses.
1. The web-tool exfiltration acceptance, re-reviewed, with the output and citation monitoring it relies on running.
1. The audit module and the upstream-log bucket deployed and receiving entries.
1. A scan of CaRDinal's Metamist project, run by someone with read access to it, confirming that no direct identifier (a
   name, a date of birth, a medical record number, an address) sits in the institution-supplied identifiers, the
   free-form metadata or the free-text phenotype notes. Metamist does not enforce de-identification, and the agent's
   access to participant metadata relies on the notes following that convention (`docs/design/tool-surface.md`).

## Alternatives considered

- Promoting dev's image into prod by copy, as `spike-infrastructure.md` §6 proposed. Rejected because dev runs
  unreviewed branch commits, so a given `main` commit usually has no dev image, and "validated on dev" would mean
  validated against a stack whose program and schema may differ from `main`'s. Making it work would need `images.yml` to
  push every `main` commit, and an identity spanning both projects' registries.
- Deploying on merge with the credential bound to `main`, as `deployment.md` proposed. Rejected because the branch
  binding admits every workflow that runs on `main`, and because it deploys without waiting for the merged commit's
  tests.
- A separate prod clu group. Rejected for now because its members would be the same operators, as argued above; it
  becomes right once prod's operators are a narrower set than dev's.
- No preview account in prod, so that no PR job can obtain a prod credential. Rejected because it removes prod's plan
  from PR review: with no second approver either, a prod infrastructure change would be seen only in the deploy log,
  after it applied.
- Previews without decrypt. Not possible: the gcpkms secrets manager decrypts on every operation, so a preview without
  decrypt fails rather than showing a partial plan.
- A separate secrets stack per environment, the remedy if the trusted-membership condition stops holding. The secret
  versions, the Slack notification channel (whose token Cloud Monitoring needs at creation) and the confidential model
  id would move to a small stack with its own KMS key, applied only by the deploy job and never previewed on a PR. The
  main stack would then hold nothing secret, which a test would enforce, and the preview account's decrypt would expose
  nothing. Deferred because it costs a second stack to operate and bring up in each environment, changes to it get no PR
  preview, and it reverses `deployment.md`'s decision that Pulumi provisions secret values; under today's condition the
  exposure it removes is accepted.

## Open questions

- A second approver for prod deploys. GitHub offers required reviewers on a private repository's Environment only on its
  Enterprise plan; the org is on Team. Without one, a maintainer's merge deploys to prod, since a maintainer's
  implementation PR merges on the author's own read ([`../design/review-policy.md`](../design/review-policy.md)), and
  nobody reads the deploy job's preview before it applies. The options are accepting that, moving to Enterprise, or an
  admin-only promotion branch gated by a ruleset, the mechanism `deployed/dev` already uses, which would put a person
  between the merge and the prod deploy.
- How CaRDinal data reaches prod: which buckets or datasets, read by which identity, and how that access is granted.
  `deployment.md` publishes each stack's config on the public mirror on the grounds that the Spike "discloses nothing
  about the data estate". Naming CaRDinal's storage in prod's config would break that, so the data path needs a design
  that keeps those identifiers out of tracked files.
- Anthropic keeps session event streams, which carry conversation content and tool results, until they are explicitly
  deleted ([`cost-monitoring.md`](../design/cost-monitoring.md)), and nothing in Themis deletes them. Whether that is
  acceptable for CaRDinal-derived content, under which agreement, and with which workspace retention setting, is settled
  in phase 4.
- The prod retention and erasure policy: how long reports, working documents and logs are kept, and how a withdrawn
  consent is honoured. A working document lives in its Analysis's repository in sheaf's bucket, which deletes no pack by
  age and keeps an overwritten or deleted object for a fixed window. A curator's browser also keeps a copy of each
  Analysis they opened, which no server-side erasure reaches. The policy should confirm or replace both. To be decided
  in the design-doc PR.
- Whether workspace separation isolates rate limits as `claude-api-wif.md` assumes. The Claude API reference documents
  Managed Agents' request limits per organization, which would let a busy dev throttle prod's session starts.
- Whether the `hello` test service ships to prod. It is harmless, and reachable by the sandbox job.
