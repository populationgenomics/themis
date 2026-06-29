# Runbook: doc-garden GitHub App setup

One-time setup for the `doc garden` workflow
([`internal-doc-garden.yml`](../../.github/workflows/internal-doc-garden.yml)),
which scans the repo's docs on a schedule and opens a fix-up PR. Its publish step
authenticates as a GitHub App so its PR triggers the required PR checks (`regex
screen`, `review + LLM screen`): a PR opened by the default `GITHUB_TOKEN` does
not emit `pull_request` events, so those checks would never run and the PR could
never merge. Until the App exists and its credentials are stored, the publish
step fails. Requires an **org owner** of `populationgenomics` (App creation and
installation) and repo admin on `themis-internal` (storing credentials). Design
context: [`doc-garden.md`](../plans/doc-garden.md).

Distinct from the `themis-mirror` App ([`mirror-app-setup.md`](mirror-app-setup.md)),
which is installed on the public `themis` repo and cannot write to
`themis-internal`.

## 1. Create the App

Open <https://github.com/organizations/populationgenomics/settings/apps/new>
(org Settings → Developer settings → GitHub Apps → New GitHub App) and set:

- **GitHub App name**: `themis-doc-garden` (the workflow's commit identity derives
  from this slug — keep it exact)
- **Homepage URL**: `https://github.com/populationgenomics/themis-internal`
  (required field; value is irrelevant)
- **Webhook**: uncheck *Active* (the App only mints tokens; it receives no events)
- **Permissions** → Repository permissions:
  - **Contents: Read and write** — push the `doc-garden/rolling` branch.
  - **Pull requests: Read and write** — open and list the fix-up PR.
  - **Do not** grant Workflows: write. The gardener never edits
    `.github/workflows/`; withholding the scope makes a workflow-file write fail
    at push rather than relying on instructions alone.
  - (Metadata: Read-only is added automatically; nothing else.)
- **Where can this GitHub App be installed?**: *Only on this account*

Click *Create GitHub App*.

## 2. Collect credentials

On the App's *General* settings page:

1. Copy the **Client ID** (a string starting with `Iv`). Not secret.
2. Under *Private keys*, click *Generate a private key*. A `.pem` file downloads
   — the only copy; GitHub does not retain it. Treat it as a secret until stored
   (step 4), then delete it.

## 3. Install the App on the internal repo

App settings → *Install App* → `populationgenomics` → *Install* → **Only select
repositories** → select **`themis-internal`** only → *Install*. Unlike the mirror
App, no branch-ruleset lock-down is needed: this App is one of several writers to
`themis-internal`, and its PRs pass through the same screen + CODEOWNERS gate as
any other before they can land.

## 4. Store credentials on `themis-internal`

`themis-internal` → Settings → Secrets and variables → Actions:

- *Variables* tab → *New repository variable*: name `DOC_GARDEN_APP_CLIENT_ID`,
  value = the Client ID from step 2.
- *Secrets* tab → *New repository secret*: name `DOC_GARDEN_APP_PRIVATE_KEY`,
  value = the **entire contents** of the `.pem` file, including the
  `-----BEGIN/END …-----` lines:

  ```shell
  gh secret set DOC_GARDEN_APP_PRIVATE_KEY \
    --repo populationgenomics/themis-internal < themis-doc-garden.*.private-key.pem
  ```

Delete the local `.pem` afterwards.

## 5. Verify

Trigger the workflow manually (Actions → *doc garden* → *Run workflow*, on `main`
so the WIF rule matches — see [`claude-api-wif.md`](claude-api-wif.md) Path C). On
a tree with drift it opens or updates the `doc-garden/rolling` PR; confirm that PR
shows the `regex screen` and `review + LLM screen` checks running (proof the App
token, not `GITHUB_TOKEN`, opened it).
