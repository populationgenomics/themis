"""The facts a data-plane call arrives with, assembled once per call (rpc-authorization.md).

An ``AuthContext`` is what the auth interceptor builds before any handler runs: the verified caller,
always; what the caller claimed to be calling as; and the Analysis binding the claim's session token
resolved to, when it named one. It is a frozen dataclass holding a proto rather than a proto itself: a
generated message field cannot be ``None``, and an unscoped call must read as unscoped rather than as
scoped to an empty Analysis; and a handler must not be able to edit the authorization it was handed.

A handler reads the call's context with :func:`current`. The interceptor binds it for the task that
runs the handler, so nothing is threaded through a signature the generated servicer base fixes.
"""

from __future__ import annotations

import contextvars
import dataclasses

from themis.rpc import auth_pb2, sandbox_options_pb2


@dataclasses.dataclass(frozen=True)
class AuthContext:
    """What one call was admitted as.

    Attributes:
        caller: The verified service-account email of the calling service, re-verified from the ID
            token Cloud Run forwarded. Never absent: a call whose token does not verify is denied
            before a context exists.
        calling_as: What the caller claimed to be calling as — an ``CallingAs`` value; ``CALLING_AS_SELF``
            when the call carried no claim. Trusted, not verified: the account is trusted to say it.
        session: The Project and Analysis the call is scoped to, resolved from the claim's session
            token; ``None`` when the claim named none or it did not resolve.
    """

    caller: str
    calling_as: sandbox_options_pb2.CallingAs
    session: auth_pb2.SessionContext | None


_current: contextvars.ContextVar[AuthContext] = contextvars.ContextVar('themis_auth_context')


def current() -> AuthContext:
    """The context of the call being served.

    Raises:
        LookupError: Called outside a call the auth interceptor gated — a handler reached some other
            way, or a test driving a servicer method directly without :func:`bind`.
    """
    try:
        return _current.get()
    except LookupError:
        raise LookupError(
            'no AuthContext is bound: current() was called outside a call the auth interceptor gated'
        ) from None


def bind(auth: AuthContext) -> contextvars.Token[AuthContext]:
    """Make ``auth`` the current context for this task; the interceptor's, and a test's, entry point."""
    return _current.set(auth)


def unbind(token: contextvars.Token[AuthContext]) -> None:
    """Restore what :func:`bind` replaced."""
    _current.reset(token)
