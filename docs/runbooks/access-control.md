# Runbook: access to a deployment

Who may use a Themis deployment is decided outside Themis, by the site that deploys it. The site writes one JSON
document per deployment, the ACL, which gives each Project two lists of email addresses, its `managers` and its
`curators`. Holding either role on any Project admits a person to the deployment. A Project is one dataset; its id is
the dataset's name, and that id is also what Themis shows for it. The auth service is the only Themis service that reads
it, and the web tier asks the auth service about every request. People sign in with a Google account through IAP, which
Google runs in front of the web service; IAP lets any Google account through and tells the app which one is calling, and
the ACL decides whether that account is admitted. [`access-control.md`](../design/access-control.md) explains the design
and why it has this shape; this runbook covers the operational side: giving someone access, setting up IAP's sign-in for
a deployment, and finding out why the auth service refused a version of the ACL.

## Where the ACL lives

The ACL is the latest version of a Secret Manager secret named `acl` in the deployment's own project:

| Deployment | ACL secret                             |
| ---------- | -------------------------------------- |
| dev        | `projects/cpg-themis-dev/secrets/acl`  |
| prod       | `projects/cpg-themis-prod/secrets/acl` |

Themis's stack creates the secret with no version (`pulumi stack output acl_secret` prints its name) and never writes
one. It grants read on the secret to the auth service's account, and the management and reading of its versions to the
site's writer, the service account `themis:aclWriter` names; these grants reach no other secret. Themis's deploy account
can read it too, as it can every secret in the stack, since it administers them all; `infra/tests/test_auth.py` fails
when any other account gains a role that reads secrets. The site adds the ACL as a new version on every change, as
compact JSON; Secret Manager refuses a version over 64 KiB. The repository holds only the writer's address, never a copy
of the ACL, because the ACL names people.

A fresh deployment's secret has no version until the site writes one, and the auth service refuses to start without a
valid ACL, so the stack deploys the secret and the auth service's account alone first
([`fresh-environment.md`](fresh-environment.md#2-first-bring-up-once-per-environment)). The stack protects the secret: a
re-created one is empty, and Themis admits nobody until the site's writer runs again, with a refresh
([`fresh-environment.md`](fresh-environment.md#when-the-acl-secret-is-re-created)).

## Giving someone access

Take an external curator, `jane.roe@example.com`, who should work on Project `asd`. The site adds her address to the
`curators` list of `asd`, which admits her to the deployment; she then sees its Analyses, and its curation shows her the
worksheets a manager assigns her. A manager of `asd`, on its `managers` list, registers its variants, assigns its
curators and reads where they diverge; a person on both lists does both. Someone on no Project is not admitted at all,
so there is no way to let a person in without a Project. The curation surface keeps no people of its own: a manager
assigns a worksheet by address, and only to someone on the Project's `curators` list. Every address is lowercase; a
version that breaks that or any other rule is rejected whole ([below](#when-the-auth-service-rejects-the-acl)).

At CPG the ACL is written by `cpg-infrastructure` from records in `cpg-infrastructure-private`. Each dataset's
`members.yaml` has two lists beside `web-access`, `themis-manager` and `themis-curator`, naming who holds each role on
that dataset in Themis, and each deployment's `project_allowlist` under `themis` in `config-infrastructure.yaml` names
the `projects.yaml` projects whose datasets the deployment may hold. Every allowed dataset with either list non-empty
becomes a Project of the same name, its managers and curators those lists' people. Each deployment's `namespace` picks
which of a dataset's two CPG namespaces the sandbox job's account, which mounts a sample's files into the agent's
sandbox, reads: `test` (`cpg-<ds>-test` and Metamist project `<ds>-test`) for dev, `main` (`cpg-<ds>-main` and Metamist
project `<ds>`) for prod. A test namespace can hold a real subset of participant data, so the allowlist, not the
namespace, is what keeps dev to open datasets. `config-infrastructure.yaml` also names each deployment's `acl_secret`,
`projects/cpg-themis-<env>/secrets/acl`, which the pipeline adds each new version to as CPG's deploy account,
`pulumi@cpg-common.iam.gserviceaccount.com`. The deploy reads back each version it adds and bills that read to Themis's
project, so Themis's stack also grants that account `roles/serviceusage.serviceUsageConsumer` there. The role lets it
see the project's enabled APIs, quotas and list of monitoring time series, but read no data. Giving someone access is
therefore a PR there. It takes effect once that pipeline has written a new version and the auth service has picked it
up, which it checks for about once a minute.

The whole path, from CPG's records to what a backend service may read, with made-up names:

![How CPG writes Themis's ACL](../design/access-control/acl-at-cpg.svg)

Removal works the same way, by deleting the entry. Someone removed from both lists of their last Project is refused at
their next request after the auth service's refresh, even though IAP still lets them through, because IAP only says who
they are. Their Analyses keep running: an agent acts for its Analysis, never for the person who started it. Removing a
Project stops its agents at their next call.

### The operator account

Anything that calls the web tier without a browser, such as the run-review loop, `tools.web_app` or a `curl` by hand,
acts as the deployment's `themis-clu` service account. The caller signs a JWT as the account, whose `aud` is the app's
origin followed by `/*`, and IAP accepts it as it accepts a person's sign-in. IAP also accepts a Google-issued ID token
whose audience is the OAuth client id IAP signs people in through, which `tools` does not use. Either way the app sees
only the account's email, and the ACL decides as it does for a person. The auth service grants that account both roles
on every Project in the ACL, so the site never lists it. It recognises the account by its address in the GCP project the
service runs in, as it does on its own rpcs. Who may act as the account is decided by who may impersonate it: Themis's
stack grants that right to one group, which it names in its config, and the site manages the group's members apart from
the ACL. At CPG it is the `themis-clu` group in `cpg-infrastructure-private`. That is the same reach the account already
has on every data-plane rpc ([`rpc-authorization.md`](../design/rpc-authorization.md)).

A `403` for this account therefore means the ACL holds no Project at all, or points at the deployment: either the call
came from another account, or the auth service runs with the wrong `THEMIS_GCP_PROJECT`. How to make such a call is in
[`hand-driving-a-service.md`](hand-driving-a-service.md#the-web-app-behind-iap).

## Setting up IAP's sign-in for a deployment

IAP sits on the `themis-web` Cloud Run service itself, so it covers every path to the service, the load balancer
included, and only IAP's own service agent may invoke the service. The program grants `roles/iap.httpsResourceAccessor`
on it to `allAuthenticatedUsers`: any signed-in Google account, service accounts included, passes IAP, and the app then
asks the auth service whether the ACL admits it. A project whose organisation enforces Domain Restricted Sharing (the
`iam.allowedPolicyMemberDomains` constraint) refuses that grant, and `pulumi up` fails on it. Dev is not under the
constraint; check prod before its first deploy, and if it is enforced, arrange an exception for the project with whoever
owns the organisation's policies:

```sh
gcloud resource-manager org-policies describe iam.allowedPolicyMemberDomains --project=cpg-themis-prod --effective
```

By default IAP signs people in through an OAuth client Google manages, which only signs in accounts inside the project's
Google Workspace organisation. External curators sign in with accounts from elsewhere, so each deployment has its own
OAuth client with an External consent screen, and the program sets it on the service's IAP settings. Google has no API
for creating a web-application OAuth client in a project, so it is created by hand in the console, once per deployment.
Do this before the stack's first `pulumi preview`, which stops on the missing client id and secret.

1. In the console for the deployment's project, open **Google Auth Platform**. If the project has no branding yet, set
   it up: an app name such as `Themis (dev)`, a support address, and the audience **External**, since an Internal app
   refuses every account outside the organisation. An External app starts in testing, where only listed test users can
   sign in, so set its publishing status to **In production**. IAP asks only for the basic scopes (`openid`, `email`,
   `profile`), which Google does not require verification for.
1. Under **Clients**, create a client of type **Web application**. Save it once to get its client id, then add one
   authorised redirect URI, `https://iap.googleapis.com/v1/oauth/clientIds/<CLIENT_ID>:handleRedirect` with the id in
   place of `<CLIENT_ID>`. That is its only redirect URI, and it needs no JavaScript origins.
1. Put the client id and secret into the stack config, from `infra/`:
   ```sh
   pulumi config set --stack dev themis:iapOauthClientId '<id>.apps.googleusercontent.com'
   pulumi config set --stack dev --secret themis:iapOauthClientSecret   # prompts for the value
   ```
   The id is no secret and sits in `Pulumi.dev.yaml` in plain text. The secret is encrypted with the stack's KMS key,
   and the deploy hands it to IAP; nothing in Themis reads it at run time.
1. Commit the stack file and deploy.

To rotate the secret, add a second one to the client in the console, set it as `themis:iapOauthClientSecret` and deploy,
then delete the old one. The client accepts both while they coexist, so sign-ins keep working throughout.

## When the auth service rejects the ACL

The auth service checks every new version of the ACL before it uses it. A version it cannot read, or one that breaks a
rule (an unknown `format`, an address with capitals, an unknown key, a duplicate entry, a Project without both lists or
with nobody on either), is rejected whole and logged at ERROR in a line that starts `acl rejected:` and names the cause.
A log-based alert on that line posts to the stack's Slack channel (`themis:slackChannel`).

What a rejection does depends on whether the service already holds a good copy. A running service keeps answering from
its last good copy until that copy is older than `themis:aclStalenessLimitSeconds` (30 minutes on dev), and from then on
refuses every request until a valid version loads: nobody can use the web app, and every agent call fails. A starting
service refuses to start without a valid ACL, so a new revision fails its startup probe and Cloud Run keeps serving the
previous one; on a fresh environment the deploy fails.

The log line says what went wrong:

```sh
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="themis-auth" AND "acl rejected:"' \
  --project cpg-themis-dev --limit 5
```

A broken rule is the writer's to fix: correct the records it generates the ACL from, and let it add a new version.
Nothing in Themis needs changing or restarting, since the service loads the next valid version at its next check. A
version it cannot read usually means the secret has no enabled version yet, or the service's read grant is missing;
`gcloud secrets versions list acl --project cpg-themis-dev` shows the first, and the secret's IAM policy
(`gcloud secrets get-iam-policy acl --project cpg-themis-dev`) should name `pulumi stack output auth_sa_email`.

## When someone cannot get in

- Google refuses the sign-in before Themis sees it: the consent screen's audience is Internal and the account is from
  outside the organisation, the app is still in testing, or the deployment's IAP still uses the Google-managed client.
- The sign-in succeeds and the app answers "Your Google account is not admitted to this Themis deployment": the address
  Google verified is on no Project's lists. That address can differ from the one the person gives you, for instance when
  they sign in to a Google account through an alias.
- The app opens but a Project is missing, or a link to it answers "not found": the person is on another Project but on
  neither of that one's lists.
- A change at the site has not arrived: the pipeline has not added a new version yet, or the auth service has not
  checked since; it does so about once a minute.
