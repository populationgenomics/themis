"""Canonicalize a ``typespec-zod`` emission against the committed ``.proto``.

``typespec-zod`` (pre-release, pinned ``0.0.0-68``) is not proto-aware: it emits an
integer proto enum as ``z.enum([0, 1, 2])`` (the values, not the member names) and a
well-known type (``google.protobuf.Timestamp``) as an opaque ``{ _extern: z.never() }``.
Neither validates the canonical proto3-JSON, which keys enums by **name** and encodes a
Timestamp as an RFC-3339 **string** (ADR 0004).

This pass repairs those two constructs using the committed ``.proto`` as the authority —
every other construct ``typespec-zod`` emits (objects, optional, default, arrays, int
ranges, url) is already correct and is passed through untouched. It is the Zod analogue of
``tools.schema.zod_reorder`` (which fixes declaration order): an automated transform over
generated code, never a hand edit. Run before ``zod_reorder``.

Fails loud rather than guessing: an enum whose proto counterpart is missing, a value with
no matching member, or a well-known other than ``Timestamp`` each raise — extending the
mapping is then a deliberate edit, not a silent mis-projection.
"""

from __future__ import annotations

import re

# proto3-JSON encodes google.protobuf.Timestamp as an RFC-3339 string; z.iso.datetime()
# validates that. Extend this map (and drop the single-well-known guard) when another
# well-known is first used.
_TIMESTAMP_ZOD = 'z.iso.datetime()'

_PROTO_ENUM = re.compile(r'enum\s+(\w+)\s*\{([^}]*)\}')
_PROTO_ENUM_MEMBER = re.compile(r'(\w+)\s*=\s*(\d+)\s*;')
_PROTO_WELLKNOWN = re.compile(r'google\.protobuf\.(\w+)')

# typespec-zod's integer-enum emission: `export const <name> = z.enum([0, 1, 2]);`.
_ZOD_INT_ENUM = re.compile(r'(export const (\w+) = z\.enum\(\[)([\d,\s]+)(\]\);)')
# typespec-zod's well-known placeholder: `z.object({ _extern: z.never() })`, any whitespace.
_ZOD_EXTERN = re.compile(r'z\.object\(\{\s*_extern:\s*z\.never\(\),?\s*\}\)')


def _proto_enum_names_by_value(proto_text: str) -> dict[str, dict[int, str]]:
    """Map each proto enum name to its ``{value: member_name}``."""
    enums: dict[str, dict[int, str]] = {}
    for name, body in _PROTO_ENUM.findall(proto_text):
        enums[name] = {int(value): member for member, value in _PROTO_ENUM_MEMBER.findall(body)}
    return enums


def _rewrite_enums(zod_text: str, enums: dict[str, dict[int, str]]) -> str:
    """Rewrite ``z.enum([ints])`` to name strings; drop ``*_unspecified`` sentinels."""

    def replace(match: re.Match[str]) -> str:
        const_name = match.group(2)
        proto_name = const_name[:1].upper() + const_name[1:]
        by_value = enums.get(proto_name)
        if by_value is None:
            raise ValueError(f'zod enum `{const_name}` has no proto enum `{proto_name}` to source names from')
        names: list[str] = []
        for token in match.group(3).split(','):
            value = int(token.strip())
            member = by_value.get(value)
            if member is None:
                raise ValueError(f'enum `{proto_name}` has no member with value {value}')
            if member.endswith('_unspecified'):
                continue
            names.append(f'"{member}"')
        return f'{match.group(1)}{", ".join(names)}{match.group(4)}'

    return _ZOD_INT_ENUM.sub(replace, zod_text)


def _rewrite_externs(zod_text: str, proto_text: str) -> str:
    """Rewrite the well-known placeholder to its zod validator (Timestamp only, for now)."""
    wellknowns = set(_PROTO_WELLKNOWN.findall(proto_text))
    unmapped = wellknowns - {'Timestamp'}
    if unmapped:
        raise ValueError(f'unmapped google.protobuf well-known type(s): {sorted(unmapped)} — extend zod_canonicalize')
    return _ZOD_EXTERN.sub(_TIMESTAMP_ZOD, zod_text)


def canonicalize(zod_text: str, proto_text: str) -> str:
    """Repair a ``typespec-zod`` emission so it validates the proto's canonical JSON.

    Args:
        zod_text: Raw ``typespec-zod`` output (``models.ts``).
        proto_text: The committed ``.proto`` for the same domain, the authority for enum
            member names and well-known field types.

    Returns:
        The repaired Zod source (still to be dependency-ordered by ``zod_reorder``).

    Raises:
        ValueError: An enum or well-known cannot be resolved against the proto.
    """
    enums = _proto_enum_names_by_value(proto_text)
    return _rewrite_externs(_rewrite_enums(zod_text, enums), proto_text)
