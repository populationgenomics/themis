"""Read a text metadata entry off an inbound call."""

from __future__ import annotations

import grpc.aio


def text_value(context: grpc.aio.ServicerContext, key: str) -> str | None:
    """The value of the first ``key`` entry on the call, or ``None`` when the call carries none.

    Raises:
        TypeError: The value arrived as bytes. gRPC delivers bytes only for a ``-bin`` key, so a
            text key with a bytes value means ``key`` was misnamed.
    """
    for entry_key, value in context.invocation_metadata() or ():
        if entry_key != key:
            continue
        if not isinstance(value, str):
            raise TypeError(f'metadata {key!r} arrived as bytes; only a -bin key carries bytes')
        return value
    return None
