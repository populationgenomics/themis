"""The metric vocabulary of operational alerting: the names and labels its producers write and its policies read.

The gRPC servers record the rpc metric, the auth service its ACL rejections, and a failed deploy its one point;
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

# The auth service's count of ACL versions it refused, by reason.
ACL_REJECTIONS = 'themis_auth_acl_rejections'
REASON_LABEL = 'reason'

# A failed deploy's one point: 1, written as the run ends.
DEPLOY_FAILED = 'themis_deploy_failed'


class AclRejectionReason(enum.StrEnum):
    """Why the auth service refused a version of the ACL: the `reason` label's values."""

    TIMEOUT = 'timeout'
    UNREADABLE = 'unreadable'
    INVALID = 'invalid'
