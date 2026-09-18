"""The session-token authn client, and the gate a data-plane server puts in front of every rpc.

``derive`` mints the per-session bearer (KMS MAC-sign of the session id); ``session`` resolves it via the
auth service (``session_resolver``) and guards a servicer body with ``require_session``. ``context``,
``rules`` and ``interceptor`` are the gate (rpc-authorization.md): the interceptor builds one
``AuthContext`` per call, derives the rule the rpc's contract declares, and denies what the rule does not
admit; a handler reads its call's context with ``context.current()``.
"""

from __future__ import annotations
