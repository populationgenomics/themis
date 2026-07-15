"""In-process HMAC session-token deriver — a test double for the KMS-backed deriver.

Its bearer does not match the KMS MAC key's output, so it is only a test double, never a deployed
authorizer (self-hosted-sandbox.md §7). Kept out of ``derive`` so the production module ships no
test-only code; the dispatcher's orchestration tests import it here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from themis.clients.auth import derive


def fixture_deriver(secret: bytes) -> derive.SessionTokenDeriver:
    """Build an in-process HMAC-SHA256 deriver (deterministic, session-specific; never a real bearer)."""

    async def _derive(session_id: str) -> str:
        mac = hmac.new(secret, session_id.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip('=')

    return _derive
