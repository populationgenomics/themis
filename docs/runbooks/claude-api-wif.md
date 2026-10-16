# Runbook: Claude API auth via Workload Identity Federation

Seven workloads call the Claude API, and none of them holds a stored `ANTHROPIC_API_KEY` (see
[`spike-infrastructure.md`](../design/spike-infrastructure.md) §4/§8). Each one exchanges an OIDC token from its own
identity provider for a short-lived Anthropic token bound to a **service account** (svac) in the Anthropic org, under a
**federation rule** that pins exactly that identity. The workloads fall into three paths by where their identity token
comes from:

- **Path A, GitHub Actions on `pull_request`:** the PR review (`internal-review.yml`) and the merge follow-ups
  (`internal-merge-followups.yml`).
- **Path B, a GCP service account:** the web app (`themis-web`, the Managed Agents client), the convert worker
  (`themis-convert-worker`, the full-text PDF transcription client), the cost exporter (`themis-cost-exporter`, the
  workspace-spend monitor of [`../design/cost-monitoring.md`](../design/cost-monitoring.md)), and the deploy workflow's
  agent step (`deploy.yml`), which presents the deploy SA's Google identity rather than a GitHub one.
- **Path C, GitHub Actions on the `main` ref:** the scheduled doc-gardening agent (`internal-doc-garden.yml`).

Registering issuers, svacs and rules needs an admin of the Anthropic org, either in the Console (**Settings → Workload
identity**) or through the Admin API (below). This is unrelated to the GitHub→GCP federation that lets Pulumi
authenticate to GCP, which is the pool and provider in
[`infra/bootstrap/bootstrap.sh`](../../infra/bootstrap/bootstrap.sh) plus `google-github-actions/auth` in the deploy and
preview workflows; that one has no Anthropic step.

## Naming, workspaces and scopes

Svacs, issuers and rules are **organization-level** objects; a workspace only activates a svac through membership. The
Anthropic org is CPG's own, so names carry the product prefix `themis-` and nothing more. A GCP project id needs its
`cpg-` prefix to be globally unique, but nothing in the org competes for these names.

**Workspaces carry the environment.** Each deploy environment has its own workspace (`themis-dev` now, `themis-prod`
when prod lands), so that dev and prod have separate spend limits, rate limits and Managed Agents resources; agents,
environments, skills and sessions all belong to a workspace. CI runs in a workspace of its own, `themis-ci`, which holds
nothing else. That gives CI its own limits, and it means a token minted for a PR-driven Claude Code run cannot reach the
dev agent or its sessions.

**Svac names follow the workspace.** An environment's svac is `themis-<env>-<workload>`, derived from the GCP service
account it stands for: `themis-web@cpg-themis-dev` becomes `themis-dev-web`. The three CI workflows share one svac,
`themis-ci`, since they are the same trust class and their spend is already split by workflow in the cost monitor. A
rule is named after its svac (`themis-dev-web-rule`), except for CI's two, which are named after the OIDC subject they
match: `themis-ci-pull-request-rule` and `themis-ci-main-rule`.

**Each rule grants the narrowest scope its workload uses.** `workspace:inference` reaches the Messages API, token
counting and the models list; `workspace:developer` adds Managed Agents (agents, environments, sessions), skills and
files. The CI workflows and the convert worker only call models, so their rules carry `workspace:inference`. The web app
opens sessions, the cost exporter reads them and the deploy step updates the agent and its skills, so theirs carry
`workspace:developer`; nothing narrower reaches sessions, and there is no read-only scope. Every svac is a **Workspace
Developer** member of its workspace. Anthropic checks that membership at token exchange, not when the rule is created,
so a missing membership shows up as a failed exchange.

The ids below are identifiers, not credentials: an exchange still needs an OIDC token that matches the rule, which
nobody outside this repo's workflows and project's service accounts can mint. They are tracked in plaintext, inline in
the workflows for Paths A and C and in `Pulumi.dev.yaml` for Path B.

| What                                        | ID                                     |
| ------------------------------------------- | -------------------------------------- |
| Organization                                | `e10fa9c3-34d4-42e7-a1ac-1d13dc023d88` |
| Workspace `themis-dev`                      | `wrkspc_01LQBHT9sifFeFz3DQiuJKB7`      |
| Workspace `themis-ci`                       | `wrkspc_01AeNbnez7whNdJhimodKZN9`      |
| Issuer `github-actions`                     | `fdis_01Gntx6scBrAeJtcxCq5nLBx`        |
| Issuer `gcp`                                | `fdis_0112F9BkK9icqCBXdtzDudYx`        |
| svac `themis-ci` (Paths A and C)            | `svac_01GPuQDLeVFJARY6KY6EgVzM`        |
| rule `themis-ci-pull-request-rule` (Path A) | `fdrl_01TEh7DGQAz9r4wEPWD99S89`        |
| rule `themis-ci-main-rule` (Path C)         | `fdrl_01MbNvREUyFT4FLf76M8qZM4`        |
| svac `themis-dev-web` (Path B)              | `svac_01XmjrcCbPooxthkbWn5JcKg`        |
| rule `themis-dev-web-rule`                  | `fdrl_019dmRVVbye6txjip13uQTaj`        |
| svac `themis-dev-convert-worker` (Path B)   | `svac_016CYiKBMujVhnR8WYnRuWDU`        |
| rule `themis-dev-convert-worker-rule`       | `fdrl_01Xjz2ApMWww5fKrB3H83AGY`        |
| svac `themis-dev-cost-exporter` (Path B)    | `svac_011Bznb6VLqXPL3gZWjebp5e`        |
| rule `themis-dev-cost-exporter-rule`        | `fdrl_01DbotnmqhPPYMMyYSWJRnKG`        |
| svac `themis-dev-deploy` (Path B)           | `svac_01AhdNfFYgH3QoMjZWqe1YsR`        |
| rule `themis-dev-deploy-rule`               | `fdrl_01JczBHeE62jFpt24KzD3oiQ`        |

CI only ever runs against the repo, never a deploy target, so prod adds no CI counterpart. It adds a `themis-prod`
workspace and the four Path B svacs and rules (`themis-prod-web`, `themis-prod-convert-worker`,
`themis-prod-cost-exporter`, `themis-prod-deploy`), each pinned to the prod project's own service account.

## Registering through the Admin API

The Console works for one-off changes. For a batch (a new environment's four svacs and rules, say), the Admin API is
less error-prone, since the rule bodies below can be sent as written. Issuer, svac and rule endpoints accept only an
OAuth token carrying `org:admin`; an Admin API key is rejected. An org admin gets one through a dedicated `ant` profile,
so that the token never replaces the everyday profile:

```sh
ant auth login --profile <name> --scope org:admin --workspace-id <any workspace id in the org>
```

The CLI binds every token to a workspace, even this one, whose endpoints are org-level, so any workspace will do.
`ant auth login` also makes the new profile the active one; switch back with `ant profile activate <everyday profile>`.
The relevant endpoints, all under `/v1/organizations/`, are `federation_issuers`, `service_accounts`,
`service_accounts/{id}/workspaces` (membership, body `{"workspace_id", "workspace_role": "workspace_developer"}`) and
`federation_rules`. API-created rules may only carry `workspace:developer` or `workspace:inference`.

In a rule, `match.subject_prefix` without a trailing `*` matches the token's `sub` exactly, and `match.claims` matches
further string claims exactly. Every rule below matches exactly; a trailing `*` would turn the subject into a prefix,
and `…:ref:refs/heads/main` is a prefix of `…/maintenance` and `…/main-experiment`.

## Path A: GitHub Actions on `pull_request`

For `internal-review.yml` and `internal-merge-followups.yml`. Both trigger on `pull_request` events (the follow-ups on
`closed`), and a `pull_request` event carries the OIDC subject `repo:populationgenomics/themis-internal:pull_request`
whatever its type, so both present the same identity and share one rule.

1. **Issuer** `github-actions`: issuer URL `https://token.actions.githubusercontent.com`, JWKS from discovery.
1. **Service account** `themis-ci`, a Workspace Developer member of `themis-ci`.
1. **Federation rule** `themis-ci-pull-request-rule`:
   ```json
   {
     "name": "themis-ci-pull-request-rule",
     "issuer_id": "fdis_01Gntx6scBrAeJtcxCq5nLBx",
     "match": {
       "subject_prefix": "repo:populationgenomics/themis-internal:pull_request",
       "audience": "https://api.anthropic.com",
       "claims": { "repository_owner": "populationgenomics" }
     },
     "target": { "type": "service_account", "service_account_id": "svac_01GPuQDLeVFJARY6KY6EgVzM" },
     "workspace_id": "wrkspc_01AeNbnez7whNdJhimodKZN9",
     "oauth_scope": "workspace:inference",
     "token_lifetime_seconds": 600
   }
   ```
   The full subject plus `repository_owner` rejects any other repo or event type. The repo is private and takes no
   external forks, which bounds what a PR-triggered token can be minted for.
1. **Wire the workflows.** Grant `id-token: write` and pass the four ids to `anthropics/claude-code-action` inline.
   Never pass `anthropic_api_key` as well: a static key outranks federation in the SDK's credential precedence and
   silently wins.
   ```yaml
   permissions:
     id-token: write
   # …
       - uses: anthropics/claude-code-action@v1
         with:
           anthropic_federation_rule_id: fdrl_01TEh7DGQAz9r4wEPWD99S89
           anthropic_organization_id:    e10fa9c3-34d4-42e7-a1ac-1d13dc023d88
           anthropic_service_account_id: svac_01GPuQDLeVFJARY6KY6EgVzM
           anthropic_workspace_id:       wrkspc_01AeNbnez7whNdJhimodKZN9
   ```

## Path B: a GCP service account

Four workloads travel this path, each under its own svac and rule, pinned to its own GCP service account: the web app,
the convert worker and the cost exporter as their Cloud Run runtime SAs, and the deploy workflow's agent step as the
deploy SA. Keeping the identities separate on both sides means that disabling one GCP account revokes only that
workload, and that Anthropic-side usage stays attributable per workload.

All four rules have the same shape. Each matches a Google service account by its numeric unique ID (the `sub` of a
Google ID token) and its `email`, uses the `gcp` issuer (issuer URL `https://accounts.google.com`, JWKS from discovery,
which covers every GCP surface), and binds the token to the environment's workspace. The web app's, in full:

```json
{
  "name": "themis-dev-web-rule",
  "issuer_id": "fdis_0112F9BkK9icqCBXdtzDudYx",
  "match": {
    "subject_prefix": "111207962341197569515",
    "audience": "https://api.anthropic.com",
    "claims": { "email": "themis-web@cpg-themis-dev.iam.gserviceaccount.com" }
  },
  "target": { "type": "service_account", "service_account_id": "svac_01XmjrcCbPooxthkbWn5JcKg" },
  "workspace_id": "wrkspc_01LQBHT9sifFeFz3DQiuJKB7",
  "oauth_scope": "workspace:developer",
  "token_lifetime_seconds": 600
}
```

Google never reissues a unique ID, so the `sub` match fails closed when an account is deleted and recreated under the
same email: the new account gets a new ID and stops matching. The `email` is the readable half of the pin, and it is
only present in the token when the token is fetched with `format=full`. The account must be user-managed, never a
default compute account.

The other three rules differ only in these values:

| Rule                             | GCP service account                                            | Svac                        | Scope                 |
| -------------------------------- | -------------------------------------------------------------- | --------------------------- | --------------------- |
| `themis-dev-convert-worker-rule` | `themis-convert-worker@cpg-themis-dev.iam.gserviceaccount.com` | `themis-dev-convert-worker` | `workspace:inference` |
| `themis-dev-cost-exporter-rule`  | `themis-cost-exporter@cpg-themis-dev.iam.gserviceaccount.com`  | `themis-dev-cost-exporter`  | `workspace:developer` |
| `themis-dev-deploy-rule`         | `themis-deploy@cpg-themis-dev.iam.gserviceaccount.com`         | `themis-dev-deploy`         | `workspace:developer` |

**A rule can only be registered against an account that already exists**, because it pins the account's unique ID. Read
it from the stack output, which is the source of truth, or from GCP:

```sh
pulumi stack output web_sa_unique_id             # also convert_worker_sa_unique_id, cost_exporter_sa_unique_id
gcloud iam service-accounts describe themis-deploy@<project>.iam.gserviceaccount.com --format='value(uniqueId)'
```

The web app's and the cost exporter's accounts are minted by the environment's first `up`, so their rules follow the
first deploy; until then their `themis:anthropic*FederationRuleId` keys hold placeholders
([`fresh-environment.md`](fresh-environment.md) §3). The convert worker's account is created by hand and adopted, so its
rule can be registered while the Pulumi change is still in review ([`fresh-environment.md`](fresh-environment.md), §
Adopting a service account created ahead of the program). The deploy SA is created by bootstrap, so its rule can precede
the first `up`.

### Runtime wiring

**The web app and the cost exporter** fetch their Google ID token from the metadata server with
`audience=https://api.anthropic.com&format=full`. The stack carries each workload's svac and rule as plaintext config
(`themis:anthropicServiceAccountId` and `themis:anthropicFederationRuleId` for the web app,
`themis:anthropicCostExporter*` for the exporter), and the `web` and `cost` components set them, with the shared
`themis:anthropicOrganizationId` and `themis:anthropicWorkspaceId`, as the four `ANTHROPIC_*` env vars. The web app's
TypeScript client pins `apiKey` and `authToken` to null alongside its credential provider, so a stray
`ANTHROPIC_API_KEY` cannot take precedence.

**The convert worker** reads the same four env vars at startup (from `themis:anthropicWorker*`), so a revision missing
one fails its startup probe rather than a conversion. It builds an `anthropic.WorkloadIdentityCredentials` per
transcription, with the ID token minted by `google.oauth2.id_token.fetch_id_token`; nothing is written to disk. The
Python SDK reads no credential environment variable when a client is given credentials explicitly, so the worker needs
no `ANTHROPIC_API_KEY` guard. Before the first conversion in a new environment, check that the deployed account is still
the one the rule pins, because a mismatch is silent until the first exchange: the revision deploys healthy and IAM is
correct. Compare `pulumi stack output convert_worker_sa_unique_id` with the rule's `subject_prefix`. They differ only if
the account was replaced, which `protect` and `retain_on_delete` on its declaration exist to prevent.

**The deploy step** applies `agents/svcv4-classifier.agent.yaml` to the Managed Agents agent after the stack
([`self-hosted-sandbox.md`](self-hosted-sandbox.md), the agent). The workflow impersonates the deploy SA through its
GitHub OIDC token under the ref-scoped WIF binding, so the path to the agent is gated exactly as the stack is
([`../design/deployment.md`](../design/deployment.md), "Who can deploy"). `google-github-actions/auth` mints the ID
token for the impersonated account (`token_format: id_token`, audience `https://api.anthropic.com`,
`id_token_include_email`) in place of a metadata server, and the SDK exchanges it as `ANTHROPIC_IDENTITY_TOKEN`. Nothing
is granted on the GCP side for this: the ref-scoped principals already hold `roles/iam.workloadIdentityUser` on the
deploy SA, which carries `iam.serviceAccounts.getOpenIdToken`. The stack exports
`themis:anthropicDeployServiceAccountId` and `themis:anthropicDeployFederationRuleId` for the workflow to read; while
either is a placeholder, every deploy's agent step fails at the exchange after the stack has applied, and the
declaration is applied by hand.

## Path C: GitHub Actions on the `main` ref

For the doc-gardening agent in `internal-doc-garden.yml`, which runs on `schedule` and `workflow_dispatch`, not
`pull_request`. Its OIDC subject is `repo:populationgenomics/themis-internal:ref:refs/heads/main`, so the Path A rule
rejects it (`400 invalid_grant`). It uses the same svac, `themis-ci`, under a second rule; a svac can carry several
rules, and widening the Path A rule to both subjects would loosen the PR pin.

The rule `themis-ci-main-rule` is the Path A body with two changes: `"name": "themis-ci-main-rule"` and
`"subject_prefix": "repo:populationgenomics/themis-internal:ref:refs/heads/main"`. A `schedule` run always carries this
subject, as does a `workflow_dispatch` on `main`, while a dispatch on any other branch carries a different ref and is
rejected. The workflow passes this rule's id with the Path A values for the org, svac and workspace.

## Verify

A successful exchange returns an `access_token` starting `sk-ant-oat01-`. The failures and their usual causes:

- `400 invalid_grant`: a claim mismatch. For GitHub, the subject's trailing segment (the event or ref); for GCP, the
  `email` claim missing because the token was not fetched with `format=full`, or an account whose unique ID changed. A
  svac that is not a member of the rule's workspace also fails here, at the exchange.
- The exchange succeeds but a later call is `403`: the call needs a wider scope than the rule grants.
