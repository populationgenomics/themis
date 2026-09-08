"""The workspace-spend monitor's exporter, and the morning report that reads the spend back.

The exporter writes Anthropic session usage, by agent, as gauges through the Telemetry API; the report reads every
producer's spend from the metrics and posts it to Slack. Two Cloud Run Jobs from one image.
"""
