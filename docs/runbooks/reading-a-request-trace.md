# Reading a request's trace

A request can pass through several Cloud Run services: the web tier calls a backend service, and sheaf calls auth to
resolve the session a call names. Each service's request log says how long that service took in total, but not where the
time went inside it. A trace answers that. The web tier, sheaf and auth each record a span around every piece of work
that leaves the process (a database query, a KMS call, a GCS request, an outgoing rpc) and export the spans to Cloud
Trace, which assembles the spans of one request into one tree.

Every outgoing call carries the trace, but only a service that records spans adds its part of the tree. The evidence and
store services and the sandbox worker record none, so a call from the web tier to evidence shows as the web tier's
client span with nothing under it.

The pipeline itself lives in [`themis/telemetry/tracing.py`](../../themis/telemetry/tracing.py) for the Python services
and [`apps/web/src/server/tracing/`](../../apps/web/src/server/tracing/) for the web server.

## Finding a trace

Cloud Run writes the trace id on every request log entry it records, and each service continues the trace id it
receives, so the request logs of all the hops a request made carry the same id. The quickest way in is from one of those
entries: open the request in the Logs Explorer, and the entry's `trace` field links to the trace in Cloud Trace.

To look for traces directly, open the
[Trace Explorer](https://console.cloud.google.com/traces/explorer?project=cpg-themis-dev) and filter on the span name
(an rpc's spans are named for its method path, such as `/themis.rpc.sheaf.Sheaf/ReadRefDoc`) or on the service name
(`themis-web`, `themis-sheaf`, `themis-auth`). Viewing traces needs `roles/cloudtrace.user` on the project.

Spans are exported in batches every few seconds, so a trace can take ten seconds or so to fill in after the request
returns.

## Reading the tree

Here is the shape of a sheaf `ReadRefDoc` call, indented by parent:

```
/themis.rpc.sheaf.Sheaf/ReadRefDoc            themis-sheaf   server span: the whole rpc
  auth.verify_caller                          themis-sheaf   the caller's ID token checked
  /themis.rpc.auth.Auth/ResolveSession        themis-sheaf   client span: the call to auth
    /themis.rpc.auth.Auth/ResolveSession      themis-auth    server span
      cloudsql.query                          themis-auth    the session_context lookup
        cloudsql.connect                      themis-auth    a new IAM-authed connection, if the pool has none idle
  gcs.get_blob                                themis-sheaf   the ref document's metadata
  gcs.download                                themis-sheaf   its bytes, pinned to that generation
```

The time in a span that none of its children covers is the service's own work, or waiting for a thread to pick the work
up. The gap between a client span and the server span under it is the network plus Cloud Run's front end. In the web
tier, Next.js opens the server span for each request, and the BFF's spans sit under it: `pg.<method>` for each database
read (with a `pg.connect` child for taking a connection from the pool, which is where the time goes when the pool has to
open one), `kms.macSign` for a session bearer, `gcs.*` for the working document, and a client span for each call to a
backend service.

A server span sometimes has a parent that the trace does not contain. Cloud Run puts a span of its own between every two
hops and records it for at most one request every ten seconds per instance, so on most requests the parent that the
server span names was never written. The span still belongs to the trace, since it carries the trace id, and it still
starts inside the client span that made the call.

## Sampling

Each stack sets the fraction of requests it traces as `themis:traceSampleRatio`, and the program passes it to every
traced service as `THEMIS_TRACE_SAMPLE_RATIO`. Dev traces every request. Every service decides from the trace id alone,
using the same rule, so a request is traced in all its hops or in none of them. Offline, with the fixture backends, no
service reads the ratio and nothing is exported.
