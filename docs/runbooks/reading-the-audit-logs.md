# Reading the audit logs

Each environment records who read what, as well as who changed what. GCP writes Admin Activity audit logs for every
change to a resource's configuration. The program adds Data Access audit logs for Secret Manager, Cloud KMS, Cloud SQL
and GCS, and turns on pgAudit, PostgreSQL's audit extension, so that every statement a database role runs is logged with
the values bound into it. [`infrastructure.md`](../design/infrastructure.md) explains why, and
[`data-retention.md`](../design/data-retention.md) says how long the entries are kept.

## Where the entries are

Data Access entries, pgAudit's included, are routed to the environment's regional log bucket, `themis-logs`. Neither
`gcloud logging read` nor the Logs Explorer searches it unless told to, so every command below names it:

```sh
LOGS='--bucket themis-logs --location australia-southeast1 --view _AllLogs'
```

In the Logs Explorer, choose **Refine scope**, then **Log view**, and select `themis-logs/_AllLogs`.

## Database statements

A pgAudit entry looks like any other Cloud SQL Data Access entry: its log is `cloudaudit.googleapis.com/data_access` and
its payload type is the generic `type.googleapis.com/google.cloud.audit.AuditLog`. What sets it apart is the method,
`cloudsql.instances.query`. The same log also holds Cloud SQL's Admin API calls, such as `cloudsql.instances.login` when
a service connects, so filter on the method to see statements only.

The statement sits under `protoPayload.request`: `user` is the database role (`themis-web@cpg-themis-prod.iam`, say),
`command` the statement's kind (`SELECT`, `INSERT`, and so on), `statement` its text with placeholders, and `parameter`
the values bound to them. `protoPayload.authenticationInfo.principalEmail` is empty on these entries, so the role is the
only identity they carry. A long statement is split over several entries, numbered by `chunkIndex` out of `chunkCount`.

Every statement one role ran, with its values:

```sh
gcloud logging read 'protoPayload.methodName="cloudsql.instances.query"
  AND protoPayload.request.user="themis-web@cpg-themis-prod.iam"' \
  --project cpg-themis-prod $LOGS --freshness 1d \
  --format='value(timestamp,protoPayload.request.command,protoPayload.request.statement,protoPayload.request.parameter)'
```

Every statement that touched a table, whoever ran it:

```sh
gcloud logging read 'protoPayload.methodName="cloudsql.instances.query" AND protoPayload.request.statement:"analyses"' \
  --project cpg-themis-prod $LOGS --freshness 1d --format='value(timestamp,protoPayload.request.user)'
```

pgAudit logs only once the extension exists in the database, which a migration creates, so a fresh environment has no
statements logged from before its first deploy.

## Secrets, keys and buckets

These entries name the caller in `protoPayload.authenticationInfo.principalEmail` and the resource in
`protoPayload.resourceName`. Who read a secret:

```sh
gcloud logging read 'protoPayload.serviceName="secretmanager.googleapis.com"
  AND protoPayload.methodName:"AccessSecretVersion"' \
  --project cpg-themis-prod $LOGS --freshness 1d \
  --format='value(timestamp,protoPayload.authenticationInfo.principalEmail,protoPayload.resourceName)'
```

The same shape works for Cloud KMS (`cloudkms.googleapis.com`, method `Decrypt`) and GCS (`storage.googleapis.com`,
methods such as `storage.objects.get`).
