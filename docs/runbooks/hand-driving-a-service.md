# Reaching a deployed Themis environment by hand

The backend services take traffic only from inside the project, so a terminal reaches them through the environment's
load balancer, where each one sits behind IAP. Every call carries two tokens. IAP checks a JWT that an account signs for
the environment's domain, and the service then checks an ID token whose `aud` is the service's own URL. A user
credential can produce neither: `gcloud auth print-identity-token --audiences=…` refuses anything that is not a service
account. Impersonation is the way across.

The `themis-clu` group may impersonate the `themis-clu` service account, which is the one account IAP admits in front of
the backend services. Membership lives in `cpg-infrastructure-private`.

The tools in this runbook need one login, your own application-default credentials from
`gcloud auth application-default login`, not an impersonated one. They impersonate the account through `google-auth` and
run no `gcloud` command, so the CLI's own login, which expires on its own schedule, plays no part in them. The `gcloud`
commands below are for calling the web app by hand.

## Call a backend service

`tools.operator_path` mints both tokens as the account and keeps them fresh, and builds a channel to the load balancer
whose every call carries them. The services speak gRPC and register no reflection, so the generated stub is the shortest
path. Calling the evidence service's literature interface looks like this:

```python
import grpc
from themis.rpc import literature_pb2, literature_pb2_grpc
from tools import clu, operator_path

project = 'cpg-themis-dev'
target = operator_path.lookup('themis-evidence', 'themis.rpc.literature.Literature', project=project)
tokens = operator_path.Tokens(target, clu.service_account(project))
stub = literature_pb2_grpc.LiteratureStub(operator_path.channel(tokens))
try:
    stub.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[doc_id]), timeout=60)
except grpc.RpcError as e:
    print(operator_path.explain(e, tokens))
```

Run it with `uv run --group tools_sheaf --group evidence python`. The target names the environment's domain, read from
its stack file, as the host the channel dials. It also names the service's URL, read from Cloud Run, as the audience of
the ID token. That URL is the one built from the service's name, the project number and the region
(`https://themis-evidence-<number>.australia-southeast1.run.app`). The service refuses a token minted for its other,
hashed URL, even though Cloud Run accepts both.

The two tokens travel as gRPC metadata. The JWT goes in `proxy-authorization: Bearer …`; its `aud` is the domain's
origin with the path wildcard, and IAP consumes it. The ID token goes in `authorization: Bearer …`; IAP passes it
through untouched, and the service verifies the caller from it. The ID token has to carry the account's `email` claim,
which an impersonated token carries only when asked (`clu.identity_token` asks). Without the claim every rpc answers
`UNAUTHENTICATED` with "the caller presented no ID token this service can verify".

## The web app, behind IAP

The app takes the same JWT IAP checks in front of the backend services, sent in `Authorization` here, since the app
needs no second token. IAP does not take a service URL as an OIDC audience, but it accepts this second token type: a JWT
the account signs itself, whose `aud` is the app's own URL. Nothing has to be registered for it (no OAuth client, no
allowlist), and `iss`/`sub` carry the account's email, which IAP passes on to the app in its own signed assertion.
`tools.web_app` signs it the same way `tools.operator_path` does.

Use the path wildcard. An `aud` of the exact URL admits that one URL only, so `https://…/` reaches `/` and gets a `401`
on every API path.

```bash
APP=https://themis-dev.populationgenomics.org.au
CLU=themis-clu@cpg-themis-dev.iam.gserviceaccount.com
NOW=$(date +%s)
printf '{"iss":"%s","sub":"%s","aud":"%s/*","iat":%s,"exp":%s}' \
  "$CLU" "$CLU" "$APP" "$NOW" "$((NOW + 600))" > /tmp/iap-jwt.json
TOKEN=$(gcloud iam service-accounts sign-jwt /tmp/iap-jwt.json /dev/stdout --iam-account="$CLU")

curl -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{}' \
  "$APP/api/rpc/themis.workbench.rpc.Workbench/ListProjects"
```

`exp` must be within 3600 seconds of `iat`; signing needs the same `serviceAccountTokenCreator` the service calls above
use, so a person who can reach a service can reach the app.

A `401` with a plain body is IAP refusing the token, and the body says which check failed:
`Audience specified does not match requested endpoint` for an `aud` that is not this URL, or lacks the wildcard the path
needs. Once IAP lets the call through, the app asks the auth service what the account may reach, as it would for a
person. The auth service grants `themis-clu`, the deployment's operator account, both roles on every Project in the ACL
file without the site listing it ([`access-control.md`](access-control.md#the-operator-account)). A `403` whose body is
the Connect error `permission_denied` / `not admitted` is the app finding the account not admitted, which for
`themis-clu` means the ACL holds no Project or the deployment is misconfigured. A call that succeeds sees every Project
in the ACL, not only the ones you see in a browser. The app's logs attribute the call to the account; which person made
it is in the signing's delegation chain in Cloud Audit Logs, logged as `signJwt`.

## The database

The instance refuses direct connections and a personal identity has no login, so `tools/psql.py` runs the connector and
connects as this account:

```bash
uv run python -m tools.psql                      # interactive
uv run python -m tools.psql -- -c 'select 1'     # one statement
```

The account is a member of the migrator role and a Cloud SQL IAM user is created `INHERIT`, so reading and writing rows
need nothing further: ownership and privilege checks both honour inheritance. That is what replaced impersonating the
deploy service account, and it is why running a migration by hand needs no second identity.

Taking the role explicitly matters for DDL, not DML — an object belongs to whoever created it, so a table created
without this would be owned by `themis-clu` rather than by the migrator that owns the rest of the schema:

```sql
SET ROLE "themis-deploy@cpg-themis-dev.iam";
```

## A git remote over the sheaf service

An Analysis's repository lives behind the `Sheaf` service, which scopes every call by a session token rather than by a
name. To inspect one from a laptop, clone it in one command:

```bash
uv run --group tools_sheaf python -m tools.clone_analysis --analysis-id "$ANALYSIS" ./"$ANALYSIS"
```

The tool derives the Analysis's session token into a file in a private temporary directory. It then serves a loopback
git remote in the same process, which presents that token on every call together with the two tokens above, minted as
`themis-clu`, through the load balancer. It clones through the remote, stops it, and deletes the temporary directory
with the token in it. The clone has the default branch checked out and every branch under `origin/`. It also holds the
tags, and the refs that pushes and sheaf itself write outside branches and tags, under their own names. One of them,
`refs/sheaf/reflog`, gains a commit on every publish, and that commit's message names each ref the publish moved.
`git log --first-parent refs/sheaf/reflog` lists the publishes newest first, down to the root entry sheaf writes when
the repository is created ([`sheaf.md`](../design/sheaf.md#the-reflog-ref-says-what-was-current)).

The clone's `origin` names the stopped server, so a fetch or a push from it fails. Pushing needs a remote that keeps
running, which takes the two steps separately, in two terminals:

```bash
uv run --group tools_sheaf python -m tools.session_token --analysis-id "$ANALYSIS" --session-token-file ~/.themis/sheaf-token.json
uv run --group tools_sheaf python -m tools.sheaf_remote --analysis-id "$ANALYSIS" --session-token-file ~/.themis/sheaf-token.json
git clone http://127.0.0.1:<port>/"$ANALYSIS"        # or `git remote set-url origin` on an existing clone
```

The port is printed by the second step. Deriving the token reads the Analysis's session id from the database and
MAC-signs it through the session-token KMS key, both as `themis-clu`. The account therefore needs
`cloudkms.signerVerifier` on that key besides the database login, and IAP access on the sheaf service's backend for the
remote. The signing grant is a decision, not a convenience: whoever can sign can derive any Analysis's session token,
the credential that scopes the sandbox's every call. `tools.sheaf_remote` keeps both tokens fresh in the token file
while it runs and removes them on Ctrl-C. Every read goes through `ReadRefDoc` and `FetchPack` and every push through
`Publish`, so a push refused by the service reads back in the hook's wording, as it would for the sandbox worker. The
token is only ever in that file; it is never on the command line or in the environment.

## What it can reach

`themis-clu` passes IAP in front of the evidence and sheaf services and the web app, whose IAP lets any Google account
through, and holds a database login with the migrator's rights; through the web app it reaches every Project in the
deployment's ACL. Where the stack opts in (`themis:cluDerivesSessionTokens`, on in dev) it also holds `signerVerifier`
on the session-token MAC key: the bearer of any live session is `HMAC(key, session_id)` (`themis.clients.auth.derive`),
and the login reads every `session_id`, so the account can act as any live session against a session-scoped service such
as `themis-sheaf`. Each of these is a named capability in the Pulumi program (`infra/themis_infra/grants.py`); the
account's grants are the `themis-clu` call sites there. The MAC call needs an access token as the account, not the ID
token above (`tools.clu.impersonated` builds one from your own login), and the key version,
`pulumi stack output session_token_signing_key` plus `/cryptoKeyVersions/1`.

The convert worker is not among them, for want of a reason rather than on principle: nothing has needed to drive a
conversion by hand yet, and doing it locally against a scratch bucket is the better first move anyway. The worker takes
internal traffic only, so adding it would also open its ingress to the load balancer, beside an operator backend for the
worker and the account's IAP access on it.

## When a call is refused

IAP refuses a call before the service sees it, and grpc-python reports that as `UNKNOWN: Stream removed`, the status a
dropped connection gets too. `operator_path.explain` tells the two apart by asking IAP directly with a plain HTTPS
request carrying the same JWT, and returns IAP's answer when it refuses. A `401` is the token refused: its body names
the check, and `Audience specified does not match requested endpoint` means an `aud` that is not the domain's origin
with the wildcard. A `403` is the account refused, for want of IAP access on that service's backend, which in practice
means the call was not made as `themis-clu`. `tools.sheaf_remote` asks the same question when a sync fails `UNKNOWN`,
and logs the answer.

Once IAP has let the call through, the service answers in gRPC statuses of its own. `UNAUTHENTICATED` means the ID token
did not verify: its `aud` is not the service's built URL (a token minted for one service is useless against another), or
it lacks the email claim. `PERMISSION_DENIED` is the rpc's own rule declining the caller. The impersonation is logged in
Cloud Audit Logs with its delegation chain, as `generateIdToken` for the ID token and `signBlob` for the IAP token the
tools sign, so "did the mint succeed, and as whom" has an answer before you start guessing.
