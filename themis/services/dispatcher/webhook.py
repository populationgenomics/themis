"""Verify an Anthropic webhook delivery via the Standard Webhooks scheme.

Anthropic signs webhooks with Standard Webhooks — the anthropic SDK's ``unwrap`` delegates to the
``standardwebhooks`` library: HMAC-SHA256 over ``id.timestamp.body`` under a ``whsec_``-prefixed key,
with a five-minute staleness window. We verify with the same library so the scheme cannot drift from
Anthropic's.
"""

from __future__ import annotations

from collections.abc import Mapping

import standardwebhooks


class SignatureError(Exception):
    """The webhook signature was missing, invalid, or stale."""


def verify(headers: Mapping[str, str], body: bytes, signing_key: str) -> object:
    """Verify the delivery's signature and return the parsed event.

    Args:
        headers: The request headers (Standard Webhooks: ``webhook-id`` / ``webhook-timestamp`` /
            ``webhook-signature``; the library matches them case-insensitively).
        body: The raw request body bytes — the signature is over these exact bytes.
        signing_key: The ``whsec_``-prefixed signing secret.

    Returns:
        The parsed JSON event; the library parses the body once the signature checks out.

    Raises:
        SignatureError: If a header is missing, the signature does not match, or the payload is stale.
        json.JSONDecodeError: If the signed body is not valid JSON (the caller maps this to 400).
    """
    try:
        return standardwebhooks.Webhook(signing_key).verify(body, dict(headers))
    except standardwebhooks.WebhookVerificationError as e:
        raise SignatureError(str(e)) from e
