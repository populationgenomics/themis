# Runbook: bring up a fresh environment

Stand up a Themis environment (`dev` or `prod`) from nothing. Two tiers: a one-time `bootstrap.sh` (the substrate Pulumi
depends on), then `pulumi up` (everything else). Design: [`infrastructure.md`](../design/infrastructure.md),
[`deployment.md`](../design/deployment.md). Program layout: [`infra/README.md`](../../infra/README.md).

Examples below use `dev` / `cpg-themis-dev`; for prod substitute `prod` / `cpg-themis-prod`. On an empty project the
first bring-up below fails: it overrides only the web image, and the program looks up the running image of every other
service and job. [`deployment.md`](../design/deployment.md#bringing-up-a-fresh-environment) describes the registry-first
bring-up that replaces it.

## Prerequisites

- The GCP project exists with billing linked (provisioned by central infra, with a budget).
- You have **Owner** on the project (to run `bootstrap.sh`) and local `gcloud` ADC:
  `gcloud auth application-default login`.
- The OAuth client IAP signs people in through exists and its id and secret are in the stack config. It is created by
  hand in the console, with an External consent screen so that accounts from outside the organisation can sign in; see
  [`access-control.md`](access-control.md#setting-up-iaps-sign-in-for-a-deployment).
- The project allows an IAM grant to `allAuthenticatedUsers`, which the IAP grant on the web service is: if the
  organisation enforces Domain Restricted Sharing (`iam.allowedPolicyMemberDomains`), the `up` fails on that grant until
  the project has an exception (same section).
- The site has a service account that will write the environment's ACL, and `themis:aclWriter` names it
  ([`access-control.md`](access-control.md#where-the-acl-lives)). The site's writer is configured with the secret it
  writes to, `projects/cpg-themis-dev/secrets/acl`, which only the first, targeted `up` creates (§2).
- The `themis-clu` group exists — see [`hand-driving-a-service.md`](hand-driving-a-service.md). It must exist before
  `pulumi up` too: the impersonation binding names it, and GCP rejects a binding whose `group:` member does not resolve.

### Local-operator KMS access (named individuals)

Local `pulumi` runs under the operator's own ADC and must decrypt/encrypt the gcpkms secrets key — including at
`stack init`. Owner does **not** grant KMS crypto operations, so each operator who runs Pulumi locally is granted
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key by name (the deploy SA gets it from bootstrap; the preview SA
gets decrypt only):

```sh
gcloud kms keys add-iam-policy-binding pulumi \
  --project=cpg-themis-dev --location=australia-southeast1 --keyring=themis \
  --member="user:<operator>@populationgenomics.org.au" \
  --role=roles/cloudkms.cryptoKeyEncrypterDecrypter
```

This is the only standing human grant on the key; revoke it to off-board an operator. Decrypts are audit-logged.

## 1. Bootstrap (once per environment)

```sh
PROJECT=cpg-themis-dev infra/bootstrap/bootstrap.sh
```

`PROJECT` is required; the script has no default project. It creates the per-environment state bucket, the KMS key, the
GitHub WIF pool, the `themis-deploy` service account (write; only the deploy workflow read from `main`, or from an
enabled ad-hoc branch, may take it; the script removes any branch-scoped binding an earlier bootstrap left) and the
`themis-preview` service account (read-only; only the preview workflow, whatever triggers it, may take it, and the
script removes the binding that admitted every pull-request workflow; a custom role holds only the program's lookups),
and network hardening (drops the default VPC + its permissive rules). Idempotent: it passes over anything that already
exists or is already gone, and stops on any other error. The deploy/preview SA emails and the WIF provider path it
prints are already wired into `.github/workflows/{deploy,preview}.yml`.

## 2. First bring-up (once per environment)

A fresh environment comes up through `tools.bring_up`, run once by an Owner from a laptop. The order it follows, and
why, is in [`deployment.md`](../design/deployment.md#bringing-up-a-fresh-environment). In short: the registry and the
ACL secret first, the images built by the deploy workflow dispatched in its images-only mode, the gene-disease dumps
written once, the site's ACL waited for, then the full `up` as the Owner, and a full deploy through the workflow, which
applies the migrations. Every step checks its own result first, so a run that stops resumes where it stopped. The tool
refuses a stack the deploy workflow has already updated, which is every environment past its first bring-up.

Before the run, every key the program reads must hold its real value except those
[§3](#3-values-that-only-exist-after-the-first-up) lists: `preview` stops at the first `config.require*` the stack does
not satisfy. These come from outside the repo:

- The OAuth client IAP signs people in through, `themis:iapOauthClientId` and `themis:iapOauthClientSecret`
  ([`access-control.md`](access-control.md)). The tool stops before the full `up` while the client id is a placeholder.
- The Anthropic workspace and the deploy account's svac and rule (round 1 of [`claude-api-wif.md`](claude-api-wif.md)),
  plus the self-hosted environment and its key, the agent, and the webhook. The webhook's URL,
  `https://<domain>/webhooks/anthropic`, is known as soon as the hostname is chosen, so it can be registered before the
  run; its signing key goes into `themis:anthropicWebhookSigningKey`.
- `themis:openalexApiKey` and `themis:semanticScholarApiKey`, the literature APIs' keys; set each with
  `pulumi config set --secret`.
- The environment's Slack app and its two channels: `themis:slackChannel` and `themis:slackChannelId` for spend,
  `themis:alertsSlackChannel` for operational alerts, the bot token as `themis:slackBotToken`. The app needs the bot
  scopes `chat:write` and `files:write` (the morning report uploads its chart), and the bot has to be invited to both
  channels, since Slack refuses a post from a bot that is not in the channel and Cloud Monitoring does not report it.
- The site's ACL config for the environment, naming `projects/<project>/secrets/acl` as its target. The site can deploy
  it as soon as the tool's first step has created the secret; the tool waits for the first version.

```sh
cd infra
pulumi login gs://cpg-themis-prod-pulumi-state
# Pass the secrets provider explicitly at init: `stack init` does not read it from the committed stack file, and
# without it Pulumi falls back to a passphrase.
pulumi stack init prod \
  --secrets-provider="gcpkms://projects/cpg-themis-prod/locations/australia-southeast1/keyRings/themis/cryptoKeys/pulumi"
cd ..
uv run python -m tools.bring_up --project cpg-themis-prod
```

The images-only and the full deploy are dispatched on `main`, so the tool needs `gh` signed in as someone who may
dispatch workflows, and it refuses to run from anything but a clean checkout of `main`'s head: the program it applies
and the images the workflow builds come from the same commit. Prod is deployed only by dispatching `deploy.yml` on
`main`; a merge does not deploy it. Every prod run, the images-only one included, first runs lint, the tests, the web
checks and both schema checks on that commit, and deploys only if they pass, so a dispatch takes as long as the slowest
of them on top of the deploy.

An existing environment whose stack does not yet hold the ACL secret takes a targeted `up` of the secret and the auth
service's account alone, then waits for the site's first version before the next deploy. Its baseline exists already,
and targeting it would make Pulumi stop on any resource that depends on it and that the program no longer declares:

```sh
THEMIS_WEB_IMAGE=gcr.io/cloudrun/hello pulumi up \
  --target 'urn:pulumi:dev::themis::themis:infra:AclSecret**' \
  --target 'urn:pulumi:dev::themis::gcp:serviceaccount/account:Account::themis-auth-runtime'
```

### When the ACL secret is re-created

The stack protects the secret, so a `destroy`, or a change that would replace it, stops until someone unprotects it
(`pulumi state unprotect`). If the secret is re-created anyway, for instance in a rebuilt environment, it starts empty,
and the auth service admits nobody until the site writes the ACL again. The site's writer must then run once with a
refresh, so that it sees the empty secret: CPG's deploy does not refresh by default, so it assumes the version it wrote
last still exists and writes nothing.

`stack init` writes a generated `encryptedkey` line into `Pulumi.dev.yaml`; commit it (inert without KMS access, safe on
the mirror). Requires the operator KMS grant from the prerequisites above.

## 3. Values that only exist after the first `up`

Some of what the program requires, the program itself produces — circular by construction, so a fresh environment cannot
declare it up front. The first `up` runs against placeholders; each real value is then read from a stack output (or
registered against one) and set, and a second `up` applies it. What each one feeds is inert or fails loudly until then —
the table says which. On a first bring-up nobody uses the environment yet, so none of it costs anything.

| Key                                                                                            | Real value                                                                                                                                                     | Inert until set                                                                                        |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `themis:anthropicFederationRuleId`                                                             | The rule registered in the Anthropic console against `pulumi stack output web_sa_unique_id` — [`claude-api-wif.md`](claude-api-wif.md) Path B                  | The BFF cannot mint a Managed-Agents token.                                                            |
| `themis:anthropicCostExporterServiceAccountId`, `themis:anthropicCostExporterFederationRuleId` | The svac and rule registered together against `pulumi stack output cost_exporter_sa_unique_id` — [`claude-api-wif.md`](claude-api-wif.md), the cost exporter   | The cost exporter fails every scheduled run at the token exchange, so the spend gauge stays empty.     |
| `themis:anthropicWorkerServiceAccountId`, `themis:anthropicWorkerFederationRuleId`             | The svac and rule registered together against `pulumi stack output convert_worker_sa_unique_id` — [`claude-api-wif.md`](claude-api-wif.md), the convert worker | The convert worker fails each paper's transcription at the token exchange, and Cloud Tasks retries it. |

One grant is the site's to make against an account the first `up` creates, and is no stack config at all: read on the
datasets the ACL names as Projects for `pulumi stack output sandbox_job_sa_email`, the account that mounts their files
into the agent's sandbox.

Any non-empty string serves as the placeholder; neither the program nor the app parses these beyond requiring them. The
remaining `themis:anthropic*` ids are set for real from the start: the web app's svac, the org and the workspace are
Anthropic-side entities that exist before any GCP service account does; and the deploy workflow's svac and rule
(`themis:anthropicDeployServiceAccountId`, `themis:anthropicDeployFederationRuleId` —
[`claude-api-wif.md`](claude-api-wif.md), the deploy workflow) pin the deploy SA, which bootstrap creates, so they too
precede the first `up`. While either deploy id is a placeholder, the deploy's agent step fails at the token exchange
after the stack has applied, and the agent's declaration is applied by hand.

```sh
pulumi config set themis:anthropicFederationRuleId fdrl_...   # from the Anthropic console
pulumi config set themis:anthropicCostExporterServiceAccountId svac_...
pulumi config set themis:anthropicCostExporterFederationRuleId fdrl_...
pulumi config set themis:anthropicWorkerServiceAccountId svac_...
pulumi config set themis:anthropicWorkerFederationRuleId fdrl_...
# Commit the stack file, then dispatch deploy.yml on main for the environment.
```

Never copy these values from another environment's `Pulumi.<stack>.yaml`. A copied federation rule is pinned to another
environment's service account, so the token exchange refuses it. The failure reads as a broken deploy rather than a
wrong constant.

### Retiring a first-deploy image placeholder

`preview` reads each Cloud Run service's or job's live image so a plan shows no spurious image change, which needs the
resource to exist. One added in the same change has none, so `preview.yml` passes a placeholder for it. Once the first
deploy creates it, delete that line: the override keeps winning otherwise, and every later preview plans the real image
back to the placeholder.

### Adopting a service account created ahead of the program

The reverse of the placeholders above: a service account whose identity a third party has to pin *before* the program
runs. Registering an Anthropic federation rule needs the GCP SA's `email` and never-reissued `unique_id`, and that
registration is an admin roundtrip outside this repo — so the SA is created by hand first and the roundtrip starts while
the Pulumi change is still in review.

Pulumi does not adopt an existing resource. Left alone, the first `up` tries to create the account, gets
`409 AlreadyExists`, and aborts the whole stack update. Import it in the same step, matching the declaration's component
parent so the URN agrees — a top-level import creates a *different* URN, which Pulumi later reads as delete-plus-create,
issuing a fresh `unique_id` and stranding the rule that pinned the old one:

```sh
gcloud iam service-accounts create themis-convert-worker --project=<project> \
  --display-name='Themis full-text convert worker runtime'
gcloud iam service-accounts describe themis-convert-worker@<project>.iam.gserviceaccount.com \
  --format='value[separator="\n"](email,uniqueId)'   # the two claims the rule pins

# …register the Anthropic svac + rule against those two values, then, once the program declares the SA:
pulumi import gcp:serviceaccount/account:Account themis-convert-worker-runtime \
  projects/<project>/serviceAccounts/themis-convert-worker@<project>.iam.gserviceaccount.com \
  --parent 'convert_worker=urn:pulumi:<stack>::themis::themis:infra:ConvertWorker::themis'
```

Import only once the declaration exists. Importing earlier leaves state holding a resource the program does not declare,
and the next `up` — including a PR preview — tries to delete it; `pulumi import` protects by default, so that update
fails instead, blocking every deploy on the stack until the declaration lands.

The SA carries `protect` and `retain_on_delete` for the same reason the import has to be parent-matched: a replacement
reissues the `unique_id` and silently breaks token exchange.

### DNS handoff (external — IT team)

```sh
pulumi stack output lb_ip
```

Give that IP to the IT team and ask for an **A record** `themis-dev.populationgenomics.org.au → <lb_ip>` (an A record
points a name at an IP; a CNAME can't). The Google-managed TLS certificate stays `PROVISIONING` until the record
resolves, then goes `ACTIVE` (minutes to ~an hour). The IP is `protect`ed and stable, so it's safe to hand out before
the cert is live.

The address depends on nothing else in the program, so it can be reserved right after bootstrap and the stack's
`stack init`, long before the first bring-up, which lets the DNS request run in parallel. The program is still evaluated
in full, so every image override has to be set even though no service is created:

```sh
for v in CONVERT_WORKER WEB AUTH HELLO GENE_DISEASE_REFRESH DISPATCHER SANDBOX_WORKER EVIDENCE SHEAF COST_EXPORTER; do
  export "THEMIS_${v}_IMAGE=gcr.io/cloudrun/hello"
done
pulumi up --stack <env> --target 'urn:pulumi:<env>::themis::gcp:compute/globalAddress:GlobalAddress::themis-ip'
gcloud compute addresses describe themis-ip --global --project=<project> --format='value(address)'
```

## 4. Hand off to CI

Once bootstrap + the first bring-up are done, CI owns deploys: PRs get a read-only preview comment; `deploy` builds the
images and `pulumi up`s when `deployed/<env>` is pushed, or when dispatched on `main`. Merging does not deploy
([`../design/deployment.md`](../design/deployment.md)).

### Enabling the ad-hoc deploy branch

Optional, and dev-only — `prod` should deploy from `main` alone. The steps are ordered: the WIF member is the credential
and the ruleset is the only thing in front of it, so granting the member first opens a window in which any repo write
can take the deploy SA ([`../design/deployment.md`](../design/deployment.md)).

1. **Confirm the branch does not exist yet.** `git ls-remote origin 'refs/heads/deployed/*'` must print nothing. A ref
   created before the ruleset is not retroactively covered by it — `Restrict creations` only blocks future creations, so
   an existing branch is simply frozen with whatever it already points at. If anything comes back, find out who pushed
   it before going further.
1. **Create the ruleset**, targeting `deployed/*` — rules `Restrict creations`, `Restrict updates`,
   `Restrict deletions`; bypass list: the **repository-admin role**, a role rather than named people. Bypass covers the
   whole ruleset, so admins keep the force-push that repointing the branch needs while everyone else cannot even create
   it. Do **not** add `Require a pull request before merging`: it would let a non-admin land content through a PR that
   `Restrict updates` would otherwise refuse.
1. **Check the ruleset actually matches.** The ruleset UI lists matching branches; a pattern that silently matches
   nothing looks configured and enforces nothing. Confirm a non-admin can neither push nor merge into `deployed/dev`.
1. **Only then grant the WIF member:**
   ```sh
   ADHOC_DEPLOY_BRANCH=deployed/dev PROJECT=cpg-themis-dev infra/bootstrap/bootstrap.sh
   ```

### Disabling it again

Re-running `bootstrap.sh` without `ADHOC_DEPLOY_BRANCH` does **not** revoke the member — the script only adds bindings,
so that an unrelated rerun cannot silently cut off a deploy path someone is using. Withdrawing it is deliberate and
manual, in this order (member first: while the member exists, the ruleset is the only thing holding the branch):

```sh
gcloud iam service-accounts remove-iam-policy-binding \
  themis-deploy@cpg-themis-dev.iam.gserviceaccount.com \
  --project=cpg-themis-dev --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/github/attribute.workflow_ref/populationgenomics/themis-internal/.github/workflows/deploy.yml@refs/heads/deployed/dev"
git push origin --delete deployed/dev
```

Leave the ruleset in place: it costs nothing and keeps the branch un-creatable, which is the safer residue.

## Tearing down

The reserved IP is `protect`ed; `pulumi destroy` refuses until you clear the protection (deliberate — it guards the
externally-bound address). The ACL secret is protected too, because a re-created one is empty until the site writes it
again ([above](#when-the-acl-secret-is-re-created)).
