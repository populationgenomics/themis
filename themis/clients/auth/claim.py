"""The claim a call's caller makes about what it is calling as, and the metadata that carries it.

Every data-plane call may carry one ``CallerClaim`` as the ``x-themis-claim-bin`` metadata
(rpc-authorization.md): what the caller is calling as, and the session token when that names a session.
The callee verifies the calling account from its ID token and trusts the claim because the account is
trusted to make it; the claim is what the call's ``AuthContext`` is hydrated from. A call carrying no
claim calls as itself.

Two clients write it and every gated server reads it, so the encoding lives here. Servers that still read
the session token as its own header do so until their in-body checks are deleted; :func:`metadata_for`
sends both while they exist.
"""

from __future__ import annotations

from collections.abc import Sequence

import grpc.aio
from google.protobuf import message

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, sandbox_options_pb2

CLAIM_METADATA = 'x-themis-claim-bin'

# The `CallingAs` values that name a session: the token is required, and a member naming one is
# satisfied only if it resolves.
SESSION_SCOPED: frozenset[sandbox_options_pb2.CallingAs] = frozenset(
    {sandbox_options_pb2.CALLING_AS_AGENT_SESSION, sandbox_options_pb2.CALLING_AS_WORKER_SESSION}
)

Metadata = tuple[tuple[str, str | bytes], ...]


class MalformedClaimError(ValueError):
    """The claim metadata does not decode as a ``CallerClaim`` — a caller-side fault, not a denial."""


def metadata_for(calling_as: sandbox_options_pb2.CallingAs, session_token: str) -> Metadata:
    """The call metadata a caller calling as ``calling_as`` within a session sends.

    Args:
        calling_as: One of the session-naming ``CallingAs`` members.
        session_token: The session the call is scoped to.

    Raises:
        ValueError: ``calling_as`` names no session; a caller calling as itself sends no claim.
    """
    if calling_as not in SESSION_SCOPED:
        raise ValueError(f'calling_as {calling_as} names no session; a caller calling as itself sends no claim')
    claim = auth_pb2.CallerClaim(calling_as=calling_as, session_token=session_token)
    return (
        (CLAIM_METADATA, claim.SerializeToString()),
        (session_mod.SESSION_TOKEN_METADATA, session_token),  # read by the in-body checks until they go
    )


def read(context: grpc.aio.ServicerContext) -> auth_pb2.CallerClaim | None:
    """The claim on the call, or ``None`` when it carries none.

    Raises:
        MalformedClaimError: The metadata is present and does not decode.
    """
    for key, value in context.invocation_metadata() or ():
        if key != CLAIM_METADATA:
            continue
        raw = value if isinstance(value, bytes) else str(value).encode()
        try:
            return auth_pb2.CallerClaim.FromString(raw)
        except message.DecodeError as e:
            raise MalformedClaimError(f'{CLAIM_METADATA} does not decode as a CallerClaim') from e
    return None


def pairs(metadata: Sequence[tuple[str, str | bytes]]) -> grpc.aio.Metadata:
    """``metadata`` as the ``grpc.aio.Metadata`` a client call takes; bytes values ride the ``-bin`` key."""
    return grpc.aio.Metadata(*metadata)  # pyright: ignore[reportArgumentType]
