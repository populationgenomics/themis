"""Metrics the Themis workloads emit: the meter provider they share, and the vocabulary the dashboards read.

`metrics` builds the OpenTelemetry pipeline — a resource the Telemetry API can place, an OTLP exporter
authenticated with Application Default Credentials, and the one-shot reader a Job flushes through (a
long-running service reads on the SDK's periodic reader). `names` is the metric and label vocabulary
every producer writes under, and `request_tokens`
the counter a direct Messages API caller feeds. Tokens are the unit throughout; dollars are derived at
query time, except where the provider computes them (`docs/design/cost-monitoring.md`).
"""
