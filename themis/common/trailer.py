"""The bound on text that travels as a gRPC status message, in the units the transport enforces it in.

A status message becomes the ``grpc-message`` trailer, and a trailer over the transport's metadata limit is dropped
whole: the caller gets ``RESOURCE_EXHAUSTED`` with the diagnosis gone, and never retries. So every message a servicer
writes is clipped before it is sent (``authored_status.abort`` does so for every marked status).
"""

from __future__ import annotations

import urllib.parse

# One interpolated part of a message; the whole of one.
DETAIL_LIMIT = 512
MESSAGE_LIMIT = 2048

_CUT_MARKER = '…'


def _wire_cost(text: str) -> int:
    r"""What ``text`` occupies in a ``grpc-message`` trailer, which carries percent-encoded UTF-8.

    ``errors='replace'`` because a JSON body can decode to a lone surrogate — ``"\\ud800"`` is a
    valid escape and yields a ``str`` that will not encode — and a message *about* a bad payload must
    not itself raise on the way out.
    """
    return len(urllib.parse.quote(text, errors='replace'))


def clipped(text: str, limit: int = DETAIL_LIMIT) -> str:
    """``text`` bounded to ``limit`` bytes *as the trailer carries them*, cut marker included.

    Encoded bytes, not characters: a request field or an upstream body may be non-ASCII, and one such
    character costs up to twelve bytes once percent-encoded. A character bound passes a message the
    transport then drops for exceeding its metadata limit, which is the outcome the bound exists to
    prevent, so the bound has to be in the units the limit is enforced in.
    """
    if _wire_cost(text) <= limit:
        return text
    budget = limit - _wire_cost(_CUT_MARKER)
    kept: list[str] = []
    used = 0
    for char in text:
        used += _wire_cost(char)
        if used > budget:
            break
        kept.append(char)
    return f'{"".join(kept)}{_CUT_MARKER}'
