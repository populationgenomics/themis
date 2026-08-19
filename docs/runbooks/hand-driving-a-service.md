# Reaching a deployed Themis environment by hand

The backend services are IAM-gated Cloud Run. Reaching one from a terminal is not a matter of credentials you already
have: Cloud Run admits an ID token whose `aud` is the service's own URL, and a user credential cannot produce one.
`gcloud auth print-identity-token --audiences=…` refuses anything that is not a service account. Impersonation is the
way across.

The `themis-clu` group may impersonate the `themis-clu` service account, which holds the invoker bindings. Membership
lives in `cpg-infrastructure-private`.

## Mint a token and call a service

```bash
SERVICE_URL=$(gcloud run services describe themis-evidence \
  --region=australia-southeast1 --project=cpg-themis-dev --format='value(status.url)')
CLU=themis-clu@cpg-themis-dev.iam.gserviceaccount.com

TOKEN=$(gcloud auth print-identity-token \
  --impersonate-service-account="$CLU" --audiences="$SERVICE_URL")
```

The services speak gRPC, so the token goes on the call as `authorization: Bearer $TOKEN` metadata over a TLS channel to
`<host>:443` — not as an HTTP header on a REST request. `grpcurl` needs the proto tree, since no service registers
reflection; the shorter path is the generated stub:

```python
import grpc
from themis.rpc import literature_pb2, literature_pb2_grpc

channel = grpc.secure_channel(f'{host}:443', grpc.ssl_channel_credentials())
stub = literature_pb2_grpc.LiteratureStub(channel)
stub.PollFullTexts(
    literature_pb2.PollFullTextsRequest(doc_ids=[doc_id]),
    metadata=(('authorization', f'Bearer {token}'),),
)
```

## The web app, behind IAP

Same account, different audience. IAP admits a token whose `aud` names the protected resource, and this backend runs a
Google-managed OAuth client, so there is no client id to use — `iap.oauth2ClientId` on the backend service is empty. In
its place IAP names the resource itself, in the same form the app already verifies assertions against
(`apps/web/src/server/adapters/live/identity.ts`): `/projects/<project number>/global/backendServices/<backend id>`.
Both halves come from the stack, the id also being `themis:iapBackendServiceId`.

```bash
PROJECT_NUMBER=$(gcloud projects describe cpg-themis-dev --format='value(projectNumber)')
BACKEND_ID=$(gcloud compute backend-services list --project=cpg-themis-dev --format='value(id)' --limit=1)
# --include-email: generateIdToken omits the email claim by default, and IAP gates on it.
TOKEN=$(gcloud auth print-identity-token --impersonate-service-account="$CLU" --include-email \
  --audiences="/projects/$PROJECT_NUMBER/global/backendServices/$BACKEND_ID")

curl -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{}' \
  https://themis-dev.populationgenomics.org.au/api/rpc/themis.workbench.rpc.Workbench/ListProjects
```

Confirm this once, the first time the account is used: it is the one assumption in this design that could not be tested
before the account existed. A `401` means IAP rejected the token itself — a wrong audience, or one minted without
`--include-email`, which are indistinguishable from the status alone. A `403` means IAP admitted the token and the
accessor binding did not.

The app authorizes on the email in the IAP assertion, and that email is the account's rather than yours — so a call
lands on the Projects `project_members` names `themis-clu@…` against, not the ones you see in a browser, and an
otherwise-correct call answers empty until such a row exists. The app's logs attribute the call to the account; which
person made it is in the impersonation's delegation chain in Cloud Audit Logs.

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

So the account can write `project_members`, which decides the Projects a signed-in user sees — worth knowing because it
is how a fresh environment gets seeded, not as a caution.

## What it can reach

`themis-clu` holds `run.invoker` on the evidence service, IAP access on the web app, and a database login with the
migrator's rights.

The convert worker is not among them, for want of a reason rather than on principle: nothing has needed to drive a
conversion by hand yet, and doing it locally against a scratch bucket is the better first move anyway. Adding it is a
one-line binding alongside the evidence one.

## When a call is refused

`PERMISSION_DENIED` with no detail is the usual shape, and it has three causes worth separating: the caller is not in
the group; the token's audience is not this service's URL (a token minted for one service is useless against another);
or the account has no `run.invoker` on the service being called. The impersonation itself is logged as `generateIdToken`
with its delegation chain, so Cloud Audit Logs answer "did the mint succeed, and as whom" before you start guessing.
