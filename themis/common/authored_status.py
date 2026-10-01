"""The mark a servicer puts on a failure whose details it wrote for its caller, and the check that reads it.

A forwarder cannot tell a servicer's own details from the text grpc synthesises for a failed channel under the same
code: an UNAVAILABLE saying that an upstream of the servicer is down reads, on the wire, like one naming the address
the forwarder itself failed to reach. So the servicer marks every failure it ends with details of its own, and the
sandbox hatch passes a failure's details to the guest only where it carries the mark. A channel-level failure carries
no trailer from any servicer, so the mark's absence is what keeps the synthesised kind unread, whatever its code.

The mark is a promise about the text, which the hatch then trusts without reading it. Marked details are read by the
agent inside the sandbox and by a browser, so they must never carry:

- an internal address: a service URL, a Cloud SQL instance name, a queue path, a host or port of ours;
- a URL with its query, which repeats the request and whatever parameters the service added to it;
- a credential, token, or anything a header carried;
- a driver's or a database's own error text, or an upstream's internal diagnostics beyond the status it returned
  and its stated reason;
- our egress address, which some upstreams echo in their error bodies.

Details that name the rpc, the upstream by its label or public host, the status it returned and what the caller sent
are what the mark is for. Text that might hold any of the above goes to the log, and the status goes unmarked.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NoReturn, cast

import grpc
import grpc.aio

from themis.common import trailer

AUTHORED_METADATA = 'x-themis-status-authored'


async def abort(context: grpc.aio.ServicerContext, code: grpc.StatusCode, details: str) -> NoReturn:
    """End the rpc under ``code``, marking ``details`` as the servicer's own text for its caller.

    ``details`` is clipped to the trailer bound here, so no marked message can be dropped for its size.

    Args:
        context: The rpc's context.
        code: The status to end it under.
        details: What the caller is told, holding none of what the module docstring lists.
    """
    await context.abort(
        code, trailer.clipped(details, trailer.MESSAGE_LIMIT), trailing_metadata=((AUTHORED_METADATA, '1'),)
    )


def authored(error: grpc.Call | grpc.aio.AioRpcError) -> bool:
    """Whether a failed call's details are a servicer's own, as ``abort`` marks them."""
    # grpc yields each metadatum as a (key, value) pair, sync and aio alike; the sync stub types it as an object.
    pairs = cast('Iterable[tuple[str, str | bytes]]', error.trailing_metadata() or ())
    return any(key == AUTHORED_METADATA for key, _ in pairs)
