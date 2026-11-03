"""The operational metrics' vocabulary as the program's alert policies read it.

The producers write these names (`themis/telemetry/operational_names.py`); the program cannot import that module, so
this is its copy, held to it name for name by `themis/telemetry/tests/test_operational_metric_contract.py`. A series
reaches a query as `prometheus.googleapis.com/<name>/<kind>`; the rpc metric's dotted names take PromQL's quoted form.
"""

from __future__ import annotations

RPC_SERVER_CALL_DURATION = 'rpc.server.call.duration'
RPC_SYSTEM_NAME_LABEL = 'rpc.system.name'
RPC_METHOD_LABEL = 'rpc.method'
RPC_RESPONSE_STATUS_CODE_LABEL = 'rpc.response.status_code'
RPC_SYSTEM_GRPC = 'grpc'

ACL_REJECTIONS = 'themis_auth_acl_rejections'
REASON_LABEL = 'reason'

DEPLOY_FAILED = 'themis_deploy_failed'

# The `reason` label's values, in the producers' order.
ACL_REJECTION_REASONS = ('timeout', 'unreadable', 'invalid')
