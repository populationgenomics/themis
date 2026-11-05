"""The metric vocabulary of operational alerting: the names and labels its producers write and its policies read.

The gRPC servers record the rpc metric, the auth service the outcome of each ACL load, and a deploy its outcome;
the Pulumi program's alert policies read them by these names (`infra/themis_infra/operational_metrics.py` carries its
own copy, which a test holds to this module). The rpc metric and its labels are OpenTelemetry's RPC semantic
conventions, dots included, so a query names them in PromQL's quoted form; Themis's own names use underscores.
"""

from __future__ import annotations

import enum

# OpenTelemetry's RPC semantic conventions: how long each call took, in seconds, by method and final status.
RPC_SERVER_CALL_DURATION = 'rpc.server.call.duration'
RPC_SYSTEM_NAME_LABEL = 'rpc.system.name'
RPC_METHOD_LABEL = 'rpc.method'
RPC_RESPONSE_STATUS_CODE_LABEL = 'rpc.response.status_code'
# The `rpc.system.name` value every Themis server records.
RPC_SYSTEM_GRPC = 'grpc'

# The Unix time of each auth instance's latest attempt to load the ACL, by outcome.
ACL_ATTEMPT = 'themis_auth_acl_attempt_timestamp_seconds'
OUTCOME_LABEL = 'outcome'

# The Unix time a deploy run finished, on a series of its own per run, by result.
DEPLOY_FINISHED = 'themis_deploy_finished_timestamp_seconds'
RESULT_LABEL = 'result'


class AclLoadOutcome(enum.StrEnum):
    """How an attempt to load a version of the ACL ended: the `outcome` label's values.

    `LOADED` is a clean load; every other value is why the version was refused.
    """

    LOADED = 'loaded'
    TIMEOUT = 'timeout'
    UNREADABLE = 'unreadable'
    INVALID = 'invalid'


class DeployResult(enum.StrEnum):
    """How a deploy run ended: the `result` label's values."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
