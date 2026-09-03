"""The round-trip gate for a mirror: parse a record, serialise it back, compare with the source.

What proto cannot distinguish from absence — nulls, null array elements, empty arrays, empty
objects — is dropped from both sides before the comparison, so a remaining difference is a key the
mirror lacks, a value it holds in another type, or a wrapping the loader gets wrong
(docs/design/proto.md, "Mirrored upstream schemas"). A null the loader does not drop never reaches
the gate: the strict parse fails first. Numbers compare by value: an integral double mirrored as an
integer field passes here and is caught only by a non-integral value in the sample.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping

from google.protobuf import json_format, message

Unwrap = Callable[[dict[str, object]], dict[str, object]]


def normalise(node: object) -> object:
    if isinstance(node, Mapping):
        kept = {key: normalise(value) for key, value in node.items()}
        return {key: value for key, value in kept.items() if value not in (None, [], {})}
    if isinstance(node, list):
        return [normalise(item) for item in node if item is not None]
    return node


def differences(source: object, back: object, path: str = '$') -> Iterator[str]:
    if isinstance(source, Mapping) and isinstance(back, Mapping):
        for key in sorted(set(source) | set(back)):
            if key not in source:
                yield f'{path}.{key}: only after the round trip'
            elif key not in back:
                yield f'{path}.{key}: lost'
            else:
                yield from differences(source[key], back[key], f'{path}.{key}')
    elif isinstance(source, list) and isinstance(back, list):
        if len(source) != len(back):
            yield f'{path}: {len(source)} items before, {len(back)} after'
        for index, (before, after) in enumerate(zip(source, back, strict=False)):
            yield from differences(before, after, f'{path}[{index}]')
    elif source != back:
        yield f'{path}: {source!r} before, {back!r} after'


def assert_lossless(source: Mapping[str, object], parsed: message.Message, unwrap: Unwrap) -> None:
    """Assert `parsed` serialises back to `source`, up to what proto cannot distinguish from absence.

    Args:
        source: The record as the index published it.
        parsed: The record loaded into its mirror; its `json_name`s are the upstream keys.
        unwrap: The inverse of the loader's wrapping, applied to the serialised JSON.
    """
    back = json_format.MessageToDict(parsed)
    found = list(differences(normalise(source), normalise(unwrap(back))))
    if found:
        raise AssertionError('the round trip is lossy:\n' + '\n'.join(found[:20]))
