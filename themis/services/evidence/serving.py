"""What every evidence servicer of the evidence image does around its backend call.

Each rpc authorizes first, then bounds its upstream work and maps a raised `errors` type onto the
status code the proto states for it. Both are identical across interfaces, so they live here and each
servicer subclasses `EvidenceServicer` alongside its generated base.

`literature` subclasses nothing here: its corpus is not session-scoped (entitlement is a deferred
non-goal), so it resolves no session. The deadline is the image's rather than the mixin's, so it
takes that from `within_deadline` directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import grpc
from google.protobuf import message

from themis.clients.auth import session as session_mod
from themis.services.evidence import errors

# The share of its caller's budget an evidence rpc gets before it must answer. Below the deadline the
# sandbox guest calls under, so an overrun arrives as this service's own DEADLINE_EXCEEDED naming the
# rpc rather than as a client-side cancellation naming nothing. A composition slower than this is cut
# rather than waited on: the per-upstream timeouts of a chained rpc still sum above it.
_RPC_DEADLINE_S = 75.0


async def within_deadline[R](context: grpc.aio.ServicerContext, rpc: str, work: Awaitable[R]) -> R:
    """Await one rpc's whole upstream work under the image's deadline, or abort DEADLINE_EXCEEDED.

    Args:
        context: The rpc's context, aborted when the budget runs out.
        rpc: The rpc's name, so an overrun names the call the caller made.
        work: Everything the rpc awaits; expiry cancels it rather than leaving it running for a
            caller that is no longer there.

    Returns:
        What `work` returned, when it returned in time.
    """
    try:
        async with asyncio.timeout(_RPC_DEADLINE_S):
            return await work
    except TimeoutError:
        await context.abort(
            grpc.StatusCode.DEADLINE_EXCEEDED,
            f'{rpc} gave up after {_RPC_DEADLINE_S:g}s: its upstreams had not answered. The request was '
            f'accepted, so reissuing it unchanged spends the same budget again.',
        )


class EvidenceServicer:
    """Authorization and error mapping for one evidence interface's servicer.

    The data served is public, so no backend takes a session id: `_require_session` is the
    authorization gate alone.
    """

    def __init__(self, session_resolver: session_mod.SessionResolver) -> None:
        self._session_resolver = session_resolver

    async def _require_session(self, context: grpc.aio.ServicerContext) -> None:
        """Resolve the request's `x-themis-session-token` metadata, or abort the rpc."""
        await session_mod.require_session(context, self._session_resolver)

    async def _response_or_abort[R: message.Message](
        self, context: grpc.aio.ServicerContext, rpc: str, response: Awaitable[R]
    ) -> R:
        """Await one backend call under the rpc deadline, mapping its failures onto status codes.

        Call it only after `_require_session` has returned: `response` is created by the caller, so
        an authorization failure raised here would leave that coroutine un-awaited.
        """
        try:
            return await within_deadline(context, rpc, response)
        except errors.UnknownVariantError as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except errors.InvalidRequestError as e:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        except errors.UnresolvedEntityError as e:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
        except errors.InconsistentSourcesError as e:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
