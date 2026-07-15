"""The session-token authn client: mint the bearer, resolve it, and guard a servicer.

``derive`` mints the per-session bearer (KMS MAC-sign of the session id); ``session`` resolves it via
the auth service (``session_resolver``) and guards a servicer with ``require_session``.
"""

from __future__ import annotations
