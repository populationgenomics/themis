"""Composite gRPC channel credentials presenting the runtime SA's ID token.

Every internal (service-to-service) gRPC call authenticates as the caller's Cloud Run service
account: TLS to the callee plus the SA's Google-signed ID token (audience = the callee URL) as
per-call credentials. Cloud Run validates the audience, so a call without a matching ID token
is rejected. The token is minted from the metadata server and refreshed on expiry by
google-auth. Shared across every internal caller.
"""

from __future__ import annotations

import grpc
from google.auth import compute_engine
from google.auth.transport import grpc as google_auth_grpc
from google.auth.transport import requests as google_auth_requests


def channel_credentials(audience: str) -> grpc.ChannelCredentials:
    """Composite channel credentials (TLS + SA ID token) for a channel to ``audience``.

    Args:
        audience: The callee's base URL; Cloud Run requires the ID token's audience to match.

    Returns:
        Channel credentials to pass to ``grpc.aio.secure_channel``.
    """
    request = google_auth_requests.Request()
    credentials = compute_engine.IDTokenCredentials(
        request, target_audience=audience, use_metadata_identity_endpoint=True
    )
    plugin = google_auth_grpc.AuthMetadataPlugin(credentials, request)
    return grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.metadata_call_credentials(plugin),
    )
