# Runbook: mirror GitHub App setup

One-time setup for the `mirror` workflow, which pushes `main` of
`themis-internal` to the public `populationgenomics/themis` repo. The workflow
authenticates as a GitHub App; until the App exists and its credentials are
stored, every mirror run fails and nothing reaches the public repo. Requires an
**org owner** of `populationgenomics` (App creation and installation); storing
the credentials (step 4) needs repo admin on `themis-internal`. Design context:
[`screen-and-mirror-workflow.md`](../plans/screen-and-mirror-workflow.md).

## 1. Create the App

Open <https://github.com/organizations/populationgenomics/settings/apps/new>
(org Settings → Developer settings → GitHub Apps → New GitHub App) and set:

- **GitHub App name**: `themis-mirror`
- **Homepage URL**: `https://github.com/populationgenomics/themis-internal`
  (required field; value is irrelevant)
- **Webhook**: uncheck *Active* (the App only mints push tokens; it receives
  no events)
- **Permissions** → Repository permissions → **Contents: Read and write**
  (Metadata: Read-only is added automatically; nothing else)
- **Where can this GitHub App be installed?**: *Only on this account*
  ("this account" = the `populationgenomics` org, which owns the App)

Click *Create GitHub App*.

## 2. Collect credentials

On the App's *General* settings page:

1. Copy the **Client ID** (a string starting with `Iv`). Not secret.
2. Under *Private keys*, click *Generate a private key*. A `.pem` file
   downloads — this is the only copy; GitHub does not retain it. Treat it as a
   secret until stored (step 4), then delete it. If lost, generate a new key
   and repeat step 4.

## 3. Install the App on the public repo

App settings → *Install App* (left sidebar) → `populationgenomics` →
*Install* → choose **Only select repositories** → select **`themis`** only →
*Install*. The App must not be installed on any other repo; its token can push
to whatever it is installed on.

## 4. Store credentials on `themis-internal`

The mirror workflow reads two values from
`populationgenomics/themis-internal` (Settings → Secrets and variables →
Actions):

- *Variables* tab → *New repository variable*:
  name `MIRROR_APP_CLIENT_ID`, value = the Client ID from step 2.
- *Secrets* tab → *New repository secret*:
  name `MIRROR_APP_PRIVATE_KEY`, value = the **entire contents** of the `.pem`
  file, including the `-----BEGIN/END RSA PRIVATE KEY-----` lines.
  Open the file in a text editor and paste it all, or use the CLI:

  ```shell
  gh secret set MIRROR_APP_PRIVATE_KEY \
    --repo populationgenomics/themis-internal < themis-mirror.*.private-key.pem
  ```

Delete the local `.pem` afterwards.

## 5. Verify

The next merge to `main` on `themis-internal` triggers a `mirror` run that
should succeed and populate `populationgenomics/themis` (the push carries the
full history; no backfill needed). To verify without waiting, re-run the most
recent failed `mirror` run from the Actions tab — but only if that run is newer
than the switch to `client-id` (a re-run executes the workflow as of its
original commit).
