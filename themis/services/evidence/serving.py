"""What every evidence servicer of the evidence image does around its backend call.

Admission is the auth interceptor's, before any handler here runs (rpc-authorization.md): the server is
built by `interceptor.gated_server`, and each rpc's contract says who it admits. What the servicers share
is what comes after — bounding the upstream work under the rpc deadline and mapping a raised `errors`
type onto the status code the proto states for it — so that lives here, and each of the nine
database-backed servicers subclasses `EvidenceServicer` alongside its generated base. The data they
serve is public, so none reads the call's session.

`literature` does not subclass this: it maps its own failures per rpc and holds its own deadline
through `within_deadline` directly, and its producer reads the call's context (`context.current()`) to
charge the spend it starts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import grpc
from google.protobuf import message

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
    """The deadline and error mapping for one evidence interface's servicer."""

    async def _response_or_abort[R: message.Message](
        self, context: grpc.aio.ServicerContext, rpc: str, response: Awaitable[R]
    ) -> R:
        """Await one backend call under the rpc deadline, mapping its failures onto status codes."""
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
