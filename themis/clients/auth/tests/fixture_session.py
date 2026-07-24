"""An in-process session resolver — a test double for the auth-service-backed resolver.

A servicer test needs a resolver to construct its servicer with; reaching a real auth service there
would test auth, not the servicer. ``resolve_fixture_session`` resolves ``GOOD_TOKEN`` to the
binding ``PROJECT_ID``/``ANALYSIS_ID`` and raises ``UnresolvedSessionError`` for anything else — the
real resolver's contract, so a servicer's UNAUTHENTICATED and PERMISSION_DENIED paths are both
reachable. The constants are the double's contract: a test asserting on the binding it produces, or
carrying its token as metadata, names them rather than repeating the literals. Kept out of
``session`` so no production symbol exposes the double.
"""

from __future__ import annotations

from themis.clients.auth import session
from themis.rpc import auth_pb2


def session_metadata(session_token: str) -> tuple[tuple[str, str], ...]:
    """The call metadata a client carries ``session_token`` in."""
    return ((session._SESSION_TOKEN_METADATA, session_token),)


GOOD_TOKEN = 'good'
GOOD_METADATA = session_metadata(GOOD_TOKEN)
PROJECT_ID = 'proj'
ANALYSIS_ID = 'ana'


async def resolve_fixture_session(session_token: str) -> auth_pb2.SessionContext:
    """Resolve ``GOOD_TOKEN`` to its binding; raise ``UnresolvedSessionError`` for any other token."""
    if session_token != GOOD_TOKEN:
        raise session.UnresolvedSessionError
    return auth_pb2.SessionContext(project_id=PROJECT_ID, analysis_id=ANALYSIS_ID)
