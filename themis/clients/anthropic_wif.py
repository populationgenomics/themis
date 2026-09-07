"""Claude API credentials by Workload Identity Federation, as a Cloud Run workload's runtime service account.

A GCP workload calls the Claude API with no stored key: the SDK exchanges the runtime service account's
Google-signed identity token for a short-lived Anthropic token under the federation rule that pins that
account (`docs/runbooks/claude-api-wif.md`, Path B). This module is the GCP half of that exchange — the
identity token minted from the Cloud Run metadata server — bound into the SDK's credential object. The
four federation identifiers are plaintext ids, not credentials; each workload reads its own from its
environment and passes them here.
"""

from __future__ import annotations

import typing

import requests
from anthropic.lib import credentials as anthropic_credentials
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token

# The Claude API audience: the identity token is minted for it, the federation rule matches on it, and
# the exchanged Anthropic token is bound to it.
AUDIENCE = 'https://api.anthropic.com'


def identity_token() -> str:
    """Mint the Google identity token the SDK exchanges, from the Cloud Run metadata server.

    `fetch_id_token` reads the metadata identity endpoint with `format=full`, so the token carries the
    `email` claim the federation rule matches alongside `sub`.

    Raises:
        google.auth.exceptions.GoogleAuthError: No metadata server, or it was unreachable or refused.
            Not an `anthropic` error, so the SDK wraps it as `anthropic.APIConnectionError`.
    """
    # The session is owned rather than left to `Request.__del__`. The cast is google-auth's loose
    # return annotation: `fetch_id_token` either returns the token or raises.
    with requests.Session() as session:
        return typing.cast('str', id_token.fetch_id_token(google_auth_requests.Request(session), AUDIENCE))


def credentials(
    *,
    federation_rule_id: str,
    organization_id: str,
    service_account_id: str,
    workspace_id: str,
) -> anthropic_credentials.WorkloadIdentityCredentials:
    """The SDK credential for this workload's federation identity, to pass as a client's `credentials=`.

    Building it touches no network; the first exchange happens on the first request.

    Args:
        federation_rule_id: The `fdrl_…` rule pinning this workload's GCP service account.
        organization_id: The Anthropic organization the rule lives in.
        service_account_id: The `svac_…` account the rule targets.
        workspace_id: The `wrkspc_…` workspace the exchanged token is scoped to.
    """
    return anthropic_credentials.WorkloadIdentityCredentials(
        identity_token_provider=identity_token,
        federation_rule_id=federation_rule_id,
        organization_id=organization_id,
        service_account_id=service_account_id,
        workspace_id=workspace_id,
    )
