"""Metrics and traces the Themis workloads emit, through Google Cloud's Telemetry API.

`telemetry_api` is what both pipelines share: the endpoint, the Application Default Credentials the channel
carries, and the detectors that place a Cloud Run service. `metrics` builds the meter pipeline — a resource
the Telemetry API can place, an OTLP exporter, and the one-shot reader a Job flushes through (a
long-running service reads on the SDK's periodic reader). `names` is the metric and label vocabulary
every producer writes under, and `request_tokens`
the counter a direct Messages API caller feeds. Tokens are the unit throughout; dollars are derived at
query time, except where the provider computes them (`docs/design/cost-monitoring.md`). `tracing` builds
the trace pipeline a service's rpcs are recorded through, one trace per request across the services it
reaches, read in Cloud Trace.
"""
