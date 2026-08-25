"""Reading a value out of an rpc response's untyped `raw` payload, at the path its contract names.

Two of the interfaces pass an upstream's JSON through untyped, because nothing the service does to
it would add anything: gnomAD's frequency blocks and VEP's per-transcript annotations. Their protos
document the paths the SVCv4 codes are read at, and a door here is what reads them — so the failure
that matters is the payload not carrying a documented path, which means the upstream's shape moved
under a contract that still claims it. That fails loudly and names what it looked for; a key the
payload carries as null is the other thing entirely, an absence the upstream is asserting, and it
comes back as None for the door to read.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping

from google.protobuf import json_format, struct_pb2


def fields(raw: struct_pb2.Struct) -> dict[str, object]:
    """A response's `raw` Struct as plain JSON values."""
    return json_format.MessageToDict(raw)


def at(payload: Mapping[str, object], path: str) -> object | None:
    """The value at a dotted path, or None where the payload states the block is absent.

    Args:
        payload: The `raw` payload, from `fields`.
        path: The dotted path the contract documents, e.g. `variant.joint.faf95.popmax`.

    Returns:
        The value, or None where any segment along the way is null — an absence the upstream states,
        which a caller reads as one rather than as a broken contract.

    Raises:
        ValueError: If a segment names a key the payload does not carry, or one whose value is not
            an object to descend into.
    """
    walked: list[str] = []
    current: object | None = payload
    for segment in path.split('.'):
        if current is None:
            return None
        if not isinstance(current, Mapping):
            raise ValueError(
                f'the payload carries no {path!r}: {".".join(walked)!r} is a {type(current).__name__}, '
                'not an object to read a key from'
            )
        if segment not in current:
            raise ValueError(
                f'the payload carries no {path!r}: {".".join([*walked, segment])!r} is absent. '
                f'Its top-level keys are {sorted(payload)}'
            )
        current = current[segment]
        walked.append(segment)
    return current


def block(payload: Mapping[str, object], path: str) -> dict[str, object] | None:
    """The object at a dotted path, or None where the payload states it is absent.

    Raises:
        ValueError: If the path is absent, or carries something other than an object.
    """
    value = at(payload, path)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f'{path!r} carries a {type(value).__name__}, expected an object')
    return value


def number(payload: Mapping[str, object], path: str) -> decimal.Decimal | None:
    """The number at a dotted path as an exact decimal, or None where the payload states none.

    The JSON number arrives as a float, and its shortest round-trip decimal is the figure the
    upstream published; `decimal.Decimal(float)` would instead carry the binary expansion, which
    compares above a threshold the published figure sits on.

    Raises:
        ValueError: If the path is absent, or carries something other than a number.
    """
    value = at(payload, path)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{path!r} carries a {type(value).__name__}, expected a number')
    return decimal.Decimal(str(value))


def count(payload: Mapping[str, object], path: str) -> int:
    """The whole number at a dotted path.

    Raises:
        ValueError: If the path is absent, states null, or carries something that is not a whole
            number. A count is what a present block always states, so a null one is a shape change.
    """
    value = at(payload, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ValueError(f'{path!r} carries {value!r}, expected a whole number')
    return int(value)


def strings(payload: Mapping[str, object], path: str) -> tuple[str, ...]:
    """The list of strings at a dotted path; an empty list where the payload states one.

    Raises:
        ValueError: If the path is absent, or carries something other than a list of strings. A null
            list is refused for the reason a null count is: an empty `filters` is a pass, and reading
            a shape change as one would let a filter-failing call score as a passing one.
    """
    value = at(payload, path)
    if not isinstance(value, list):
        raise ValueError(f'{path!r} carries {value!r}, expected a list of strings')
    stated: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f'{path!r} carries {item!r} among its strings')
        stated.append(item)
    return tuple(stated)
