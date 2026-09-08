"""The metrics Claude Code writes from CI, as the program's dashboard and alert queries read them.

The two names are Claude Code's own OpenTelemetry instruments, as its monitoring reference names them: a token
counter by `type` (the same four values as `cost_metrics.TOKEN_TYPES`) and `model`, and a cost counter in USD by
`model` that Claude Code computes itself from its own price table. They keep their dots, so a PromQL selector
over them takes the UTF-8 quoted form (`cost_promql.selector`). No Themis producer stands behind them, so unlike
`cost_metrics.py` no contract test holds them: the vocabulary is Anthropic's.

The label is Google's, and the contract a workflow that runs Claude Code meets: it sets the OpenTelemetry resource
attribute `service.namespace` to its own name — nothing else fills it on a GitHub runner — and the Telemetry API,
filing the series on the `prometheus_target` resource, maps that attribute to the `namespace` label and prefixes it
onto `job` (`<workflow>/claude-code`). So `namespace` is how a series says which workflow spent.
"""

from __future__ import annotations

TOKEN_USAGE = 'claude_code.token.usage'  # noqa: S105 — a metric name, not a credential
COST_USAGE = 'claude_code.cost.usage'

NAMESPACE_LABEL = 'namespace'
