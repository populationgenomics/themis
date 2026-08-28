# Runbook: Claude API auth via Workload Identity Federation

Four workloads call the Claude API and authenticate by **WIF** — no stored `ANTHROPIC_API_KEY` (see
[`spike-infrastructure.md`](../design/spike-infrastructure.md) §4/§8):

1. **GitHub Actions** — the `claude-code-action` PR review (`internal-review.yml`).
1. **GCP Cloud Run** — the web app (`themis-web`), the Managed-Agents client.
1. **GCP Cloud Run** — the convert worker (`themis-convert-worker`), the full-text PDF-OCR client; its own svac and
   rule, sharing the org and workspace.
1. **GitHub Actions, scheduled** — the doc-gardening agent (`internal-doc-garden.yml`); reuses workload 1's service
   account via a second federation rule (Path C), since a scheduled run's OIDC subject differs from a PR's.

Each exchanges an OIDC token from its own identity provider for a short-lived Anthropic token bound to a **service
account** in the Anthropic org. Configure in the Claude Console (**Settings → Workload identity → Connect workload**) or
via the Admin API; requires admin on the Anthropic org.

Distinct from the GitHub→GCP WIF that lets Pulumi authenticate to GCP (that is the pool/provider in
[`infra/bootstrap/bootstrap.sh`](../../infra/bootstrap/bootstrap.sh) plus `google-github-actions/auth` in the
deploy/preview workflows) — different relying party, no Console step.

## Naming

Anthropic **service accounts (svacs) are organization-level** — a workspace only activates one by membership, it does
not namespace its name. The Anthropic org spans the whole hosting institute (wider than CPG), so svac names carry the
full **`cpg-themis-`** product prefix to be unambiguous org-wide (unlike GCP SA emails, where the `cpg-themis-dev`
project already scopes the name):

- Env-scoped workloads (the web app and the convert worker — dev and prod attribute usage and rate limits separately)
  **encode the env**: `cpg-themis-dev-web`.
- Repo-scoped workloads (the PR review — one process regardless of deploy target) stay env-neutral:
  `cpg-themis-ci-review`.

The convert worker's svac breaks the first rule: it was registered as `cpg-themis-convert-worker`, with no env segment.
Nothing depends on the name — the rule matches on claims, and the stack carries ids — but a prod convert worker cannot
get its name by swapping a segment, and needs one chosen at that point.

Themis uses **per-env workspaces** so dev and prod carry separate workspace-level spend and rate limits:
**`cpg-themis-dev`** now, a separate **`cpg-themis-prod`** when prod lands. Env-scoped workloads live in their env's
workspace — the env boundary rides on the **workspace**, and each federation rule carries that env's `workspace_id`.
Repo-scoped workloads (the PR review, doc-garden) run regardless of deploy target and sit in `cpg-themis-dev`, so every
rule below carries `cpg-themis-dev`'s `workspace_id` until the prod web app adds its `cpg-themis-prod` counterpart. GCP
service-account emails stay env-neutral because the project encodes the env:
`themis-web@cpg-themis-dev.iam.gserviceaccount.com`.

The `wrkspc_…` / `svac_…` / `fdrl_…` / `fdis_…` and the **organization ID** are identifiers, **not credentials** — an
exchange still requires a matching OIDC token, which can't be forged for this repo. So they are tracked **plaintext**,
same as the GCP project/domain/group already in `Pulumi.dev.yaml`: Path A inline in `internal-review.yml`, Path B in
`Pulumi.dev.yaml` (read identically by local and CI `pulumi up`). The concrete values:

| What                                                      | ID                                     |
| --------------------------------------------------------- | -------------------------------------- |
| Organization                                              | `0c504942-5311-4fc0-a3a3-1f6a53666205` |
| Workspace `cpg-themis-dev`                                | `wrkspc_014YcYcGz7XBbARzLRHwvhZt`      |
| svac `cpg-themis-ci-review` (Path A)                      | `svac_01KJXbuSvwDHvT8PFQkU7nef`        |
| rule `cpg-themis-ci-review-rule` (Path A)                 | `fdrl_01PkjWwWtFLfGFoRXboKaLjR`        |
| rule `cpg-themis-ci-review-main-rule` (Path C, same svac) | `fdrl_01KdBLWjujcYsH9s9j1K63Wb`        |
| svac `cpg-themis-dev-web` (Path B)                        | `svac_01ELDvWbrGpDNhcDdAaYniGJ`        |
| rule `cpg-themis-dev-web-rule` (Path B)                   | `fdrl_01JXLFyrG8PnJ62qPFzTmp4P`        |
| svac `cpg-themis-convert-worker` (Path B)                 | `svac_013a2rMRQihX3fzRKDnk7o2v`        |
| rule `cpg-themis-convert-worker-rule` (Path B)            | `fdrl_01GQqwA9kP4Fve93ycq2q6kE`        |

These rows are the **dev** set. CI (the review and doc-garden) is repo-scoped and only ever runs against dev, so prod
adds no ci-review counterpart — just its own `cpg-themis-prod` workspace, a `cpg-themis-prod-web` svac + rule, and a
convert-worker svac + rule pinned to the prod worker's own service account.

## Path A — GitHub Actions → Claude API

For the `claude-code-action` review in `internal-review.yml`.

1. **Issuer** (skip if already registered): `github-actions`, issuer URL `https://token.actions.githubusercontent.com`,
   JWKS = discovery.
1. **Service account**: `cpg-themis-ci-review` (`svac_01KJXbuSvwDHvT8PFQkU7nef`); add to the `cpg-themis-dev` workspace.
1. **Federation rule** `cpg-themis-ci-review-rule` (`fdrl_01PkjWwWtFLfGFoRXboKaLjR`) — pin to this repo's PR runs:
   ```json
   {
     "match": {
       "audience": "https://api.anthropic.com",
       "claims": {
         "sub": "repo:populationgenomics/themis-internal:pull_request",
         "repository_owner": "populationgenomics"
       }
     },
     "target": { "type": "service_account", "service_account_id": "svac_01KJXbuSvwDHvT8PFQkU7nef" },
     "workspace_id": "wrkspc_014YcYcGz7XBbARzLRHwvhZt",
     "oauth_scope": "workspace:developer",
     "token_lifetime_seconds": 600
   }
   ```
   `internal-review.yml` triggers on `pull_request`, so the OIDC `sub` is exactly
   `repo:populationgenomics/themis-internal:pull_request`. Match it exactly (not a prefix — see Path C); the full
   subject plus `repository_owner` block any other repo or event type. (Private repo + no external forks bounds the
   PR-token vector.)
1. **Wire the workflow** — in `internal-review.yml`: grant `id-token: write`, pass the federation inputs inline
   (identifiers, not secrets), and drop the key (a static key outranks federation in the SDK credential precedence and
   silently wins):
   ```yaml
   permissions:
     id-token: write
   # …
       - uses: anthropics/claude-code-action@v1
         with:
           anthropic_federation_rule_id: fdrl_01PkjWwWtFLfGFoRXboKaLjR
           anthropic_organization_id:    0c504942-5311-4fc0-a3a3-1f6a53666205
           anthropic_service_account_id: svac_01KJXbuSvwDHvT8PFQkU7nef
           anthropic_workspace_id:       wrkspc_014YcYcGz7XBbARzLRHwvhZt
   ```

## Path B — the web app → Claude API

For the web app (`themis-web`), the Managed-Agents control-plane client. Its identity is the web service's runtime SA,
`themis-web@cpg-themis-dev.iam.gserviceaccount.com` (provisioned by the `web` module); the Anthropic client lands when
the BFF does.

1. **Issuer** (once): `gcp`, issuer URL `https://accounts.google.com`, JWKS = discovery (covers all GCP surfaces).
1. **Service account**: `cpg-themis-dev-web` (`svac_01ELDvWbrGpDNhcDdAaYniGJ`); add to the `cpg-themis-dev` workspace.
1. **GCP SA unique ID** (the stable `sub`) — the SA is Pulumi-managed (`web` module), so read it from the stack output
   (or `gcloud`):
   ```sh
   pulumi stack output web_sa_unique_id   # or: gcloud iam service-accounts describe <email> --format='value(uniqueId)'
   ```
   For `cpg-themis-dev` this is currently `111207962341197569515` — a snapshot; the `pulumi stack output` above is the
   source of truth (the literal can drift if the SA is recreated). On a fresh environment the SA does not exist until
   the first `up`, so `themis:anthropicFederationRuleId` holds a placeholder until the rule below is registered —
   [`fresh-environment.md`](fresh-environment.md) §3.
1. **Federation rule** `cpg-themis-dev-web-rule` (`fdrl_01JXLFyrG8PnJ62qPFzTmp4P`) — match `sub` + `email` (Google's
   `sub` has no stable prefix; never use `subject_prefix`):
   ```json
   {
     "match": {
       "audience": "https://api.anthropic.com",
       "claims": {
         "sub": "111207962341197569515",
         "email": "themis-web@cpg-themis-dev.iam.gserviceaccount.com"
       }
     },
     "target": { "type": "service_account", "service_account_id": "svac_01ELDvWbrGpDNhcDdAaYniGJ" },
     "workspace_id": "wrkspc_014YcYcGz7XBbARzLRHwvhZt",
     "oauth_scope": "workspace:developer",
     "token_lifetime_seconds": 600
   }
   ```
   `sub` (the never-reused unique ID) survives a delete/recreate-with-same-email; `email` is the readable pin. The SA
   must be user-managed (not the GCE default).
1. **Runtime** — the web app builds the Anthropic client with `WorkloadIdentityCredentials`, whose token provider
   fetches the Google ID token from the metadata server with `audience=https://api.anthropic.com&format=full`
   (`format=full` is required so the token carries the `email` claim). When the Anthropic client lands, the `web` Pulumi
   component reads these four IDs from **plaintext stack config** in `Pulumi.<stack>.yaml`
   (`themis:anthropicFederationRuleId`, `themis:anthropicOrganizationId`, `themis:anthropicServiceAccountId`,
   `themis:anthropicWorkspaceId`) and sets them as Cloud Run env vars (`ANTHROPIC_FEDERATION_RULE_ID` etc.) — read
   identically by local and CI `pulumi up`. Ensure `ANTHROPIC_API_KEY` is unset (it outranks federation).

## Path B — the convert worker → Claude API

For the full-text convert worker (`themis-convert-worker`,
[`../../infra/themis_infra/convert.py`](../../infra/themis_infra/convert.py)), which transcribes a paper's PDF when the
OA ladder serves no usable XML. A distinct workload, so it has its own svac and rule rather than sharing the web app's;
the org and workspace are shared. Its identity is the worker's runtime service account,
`themis-convert-worker@<project>.iam.gserviceaccount.com`.

Both halves are registered (the two rows above), so for dev this describes what exists rather than steps to repeat; a
new environment repeats Path B above against its own worker service account. The rule is the web app's shape: `sub` +
`email` matched against that service account, targeting svac `cpg-themis-convert-worker`. The account is hand-created
and adopted for exactly that reason — [`fresh-environment.md`](fresh-environment.md) § Adopting a service account
created ahead of the program — since the rule needs its `email` and `unique_id` before the program declares it.

The stack carries the two ids as `themis:anthropicWorkerServiceAccountId` and `themis:anthropicWorkerFederationRuleId`,
`Worker`-qualified siblings of the web app's unqualified pair; `convert.py` sets them, with the shared org and
workspace, as the container's four `ANTHROPIC_*` env vars.

**Runtime.** The worker reads the four ids at startup, so a revision missing one fails its startup probe rather than a
conversion, and builds an `anthropic.WorkloadIdentityCredentials` for each transcription — the Google identity token
minted per exchange by `google.oauth2.id_token.fetch_id_token`, which reads the metadata identity endpoint with
`format=full` as Path B above. Nothing is written to disk and nothing is refreshed on a timer.

The worker needs no `ANTHROPIC_API_KEY` guard of its own: the Python SDK consults no credential environment variable
when the client is constructed with one, so a stray key cannot reach it. The web app's TypeScript client gets the same
property by pinning `apiKey` and `authToken` null alongside its credential provider.

**Before the first conversion in a new environment**, confirm the deployed account is still the one the rule pins — a
mismatch is silent, because the revision deploys healthy, IAM is correct, and nothing fails until the first token
exchange:

```sh
pulumi stack output convert_worker_sa_unique_id
gcloud iam service-accounts describe themis-convert-worker@<project>.iam.gserviceaccount.com \
  --format='value(uniqueId)'   # the same value, read from GCP rather than from state
```

against the `sub` claim on the rule. They differ only if the account was replaced, which `protect` and
`retain_on_delete` on the declaration exist to prevent.

## Path C — GitHub Actions (scheduled, `main` ref) → Claude API

For the scheduled doc-gardening agent in `internal-doc-garden.yml`. It runs on `schedule` / `workflow_dispatch`, not
`pull_request`, so its OIDC `sub` is `repo:populationgenomics/themis-internal:ref:refs/heads/main` — Path A's rule,
pinned to the `…:pull_request` subject, rejects it (`400 invalid_grant`).

Reuse the Path A service account `cpg-themis-ci-review` (`svac_01KJXbuSvwDHvT8PFQkU7nef`) and add **one more rule**
targeting it (a svac carries multiple rules). Do not widen Path A's rule to cover both subjects — that loosens the
PR-review pin.

Federation rule `cpg-themis-ci-review-main-rule` — pin to this repo's `main`-ref runs:

```json
{
  "match": {
    "audience": "https://api.anthropic.com",
    "claims": {
      "sub": "repo:populationgenomics/themis-internal:ref:refs/heads/main",
      "repository_owner": "populationgenomics"
    }
  },
  "target": { "type": "service_account", "service_account_id": "svac_01KJXbuSvwDHvT8PFQkU7nef" },
  "workspace_id": "wrkspc_014YcYcGz7XBbARzLRHwvhZt",
  "oauth_scope": "workspace:developer",
  "token_lifetime_seconds": 600
}
```

Match the **exact** `sub`, not a prefix: `schedule` always runs on `main` and a `workflow_dispatch` on `main` carries
this exact subject, while any other branch's ref differs and is rejected. A `subject_prefix` here would be a footgun —
`…:ref:refs/heads/main` is a prefix of `…/maintenance`, `…/main-experiment`, etc., so those branches would satisfy it.
(In the Console UI the subject is exact by default; a trailing `*` turns it into a prefix — do not add one.) The rule's
id (`fdrl_01KdBLWjujcYsH9s9j1K63Wb`) is wired into `internal-doc-garden.yml` as `anthropic_federation_rule_id`; the
other three inputs (org, svac, workspace) are the Path A values above.

## Verify

A successful exchange returns an `access_token` starting `sk-ant-oat01-`. On `400 invalid_grant` the usual cause is a
claim mismatch — GitHub: the `sub` trailing segment; GCP: `email` missing because the token was not fetched with
`format=full`.
