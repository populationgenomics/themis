"""Rendering a whole response message, for code-mode snippets to read what a service actually answered.

Guest-side: runs inside the postern sandbox, shipped into the guest rootfs as ``themis.agent.display`` alongside
``themis.agent.services``. A snippet that prints the response fields it thought to name reads only the answer it
expected; the field carrying the rest — the reason a lookup missed, a status the caller did not anticipate — goes
unread, and the service is reported as silent when it answered. ``show`` removes the choice: it renders every set
field of any protobuf message.

Printing a response whole is only safe if one oversized field can neither bury the rest nor break the rendering,
which is why this is not ``print(resp)``. A ``google.protobuf.Struct`` (each source returns its full upstream
payload in ``raw``, up to megabytes), a long text field (a curated mechanism statement, a VCEP criterion's
specification text) and a ``google.protobuf.Any`` are replaced by a marker naming their size; a ``Struct``'s marker
keeps its top-level keys, so the payload stays navigable. The value itself is untouched on the message, reachable as
``resp.raw`` / ``resp.text``.

Four properties make it safe to reach for without looking first. An elided field still renders as *set*. A message
holding nothing but defaults says so rather than rendering as the empty string. Unknown fields print rather than being
dropped, since a service deployed ahead of this image sends fields the committed descriptor does not know. In each case
a field that read as absent would be one the model concluded the service had not answered.

The fourth is a bound on the whole rendering, and it is the one that is not free. Per-field elision cannot deliver it:
no field walk reaches an unknown field, and thousands of individually short elements add up. So past the budget the
*head* of the rendering is what survives — the tail is cut on a line boundary, whatever submessage the cut left open
is closed, and a marker states how much was rendered. Fields past the cut do read as absent, and that marker is the
only thing saying otherwise, which is why the budget is set well above any ordinary response. Unknown-field content
is given up before known content is, but only when rendering it would cost a large share of the budget; below that it
stays, being the likeliest of all to carry the unexpected answer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, MutableMapping, MutableSequence
from typing import cast

from google.protobuf import any_pb2, descriptor, descriptor_pool, message, struct_pb2, text_format

# The size a field may reach before it is summarised: characters for a string, bytes for a bytes field, serialized
# bytes for a Struct.
DEFAULT_MAX_FIELD_SIZE = 2000
DEFAULT_MAX_CHARS = 20_000
# The body a rendering must have room for once its header is paid for.
_MIN_BODY_CHARS = 400
# Room reserved for the truncation marker, whose own length depends on the sizes it names.
_MARKER_ALLOWANCE = 80
# Unknown-field content is kept while it costs at most this fraction of the budget.
_UNKNOWN_SHARE = 4

_TEXT_TYPES = (descriptor.FieldDescriptor.TYPE_STRING, descriptor.FieldDescriptor.TYPE_BYTES)
# A proto2 group is a submessage under a different wire type; descending into one is the same job.
_MESSAGE_TYPES = (descriptor.FieldDescriptor.TYPE_MESSAGE, descriptor.FieldDescriptor.TYPE_GROUP)
_ELIDED_KEY = '<elided>'
_SUMMARISED_KEYS = 12
# A string written into a marker — a map key, an `Any`'s type_url — buries the fields around it as readily as
# any value, so it is cut the same way.
_MAX_SHORT_CHARS = 64
_SHORT_DIGEST_CHARS = 8
_NOTHING_SET = '<nothing set>\n'
# Matched by full name, not isinstance: a message built from a descriptor pool of its own is not an instance of the
# generated well-known type, and would reach the field walk with its payload unelided.
_STRUCT_NAME = 'google.protobuf.Struct'
_LIST_VALUE_NAME = 'google.protobuf.ListValue'
_VALUE_NAME = 'google.protobuf.Value'
_JSON_NAMES = frozenset({_STRUCT_NAME, _LIST_VALUE_NAME, _VALUE_NAME})
_ANY_NAME = 'google.protobuf.Any'


def show(
    msg: message.Message, *, max_field_size: int = DEFAULT_MAX_FIELD_SIZE, max_chars: int = DEFAULT_MAX_CHARS
) -> None:
    """Print ``msg`` whole, with its oversized fields summarised. Reach for this on every response.

    Args:
        msg: Any protobuf message — a whole rpc response, not fields picked out of one.
        max_field_size: The size a field may reach before it is summarised (see `render`).
        max_chars: The size the whole rendering may reach (see `render`).
    """
    print(render(msg, max_field_size=max_field_size, max_chars=max_chars), end='')


def render(
    msg: message.Message, *, max_field_size: int = DEFAULT_MAX_FIELD_SIZE, max_chars: int = DEFAULT_MAX_CHARS
) -> str:
    """Render ``msg`` as protobuf text format, with its oversized fields replaced by size markers.

    Args:
        msg: Any protobuf message.
        max_field_size: The size a field may reach before it is summarised — characters for a
            string, bytes for a bytes field, serialized bytes for a ``Struct`` or an ``Any``.
        max_chars: The size the whole rendering may reach, header included. Past it the tail is cut
            where no submessage is left open, and a marker states how much of the body was rendered.

    Returns:
        The rendering, headed by the message's full name and at most ``max_chars`` characters long.
        Within the budget every field the message sets appears — the unknown ones too — and an
        elided field appears as a marker naming its size, so none of them reads as absent. A message
        whose fields all hold their default says so, rather than rendering empty. Past the budget the
        head is what survives: fields beyond the cut are absent from the rendering, and the trailing
        marker is what says so.

    Raises:
        ValueError: If ``max_field_size`` is not positive, or ``max_chars`` leaves too little room
            for a body once this message's full name is paid for.
    """
    if max_field_size < 1:
        raise ValueError(f'max_field_size must be positive, got {max_field_size}')
    header = f'{msg.DESCRIPTOR.full_name}\n'
    budget = max_chars - len(header)
    if budget < _MIN_BODY_CHARS:
        raise ValueError(
            f'max_chars={max_chars} leaves {budget} characters for the body of {msg.DESCRIPTOR.full_name}, '
            f'which needs at least {_MIN_BODY_CHARS}'
        )
    summarised = type(msg)()
    summarised.CopyFrom(msg)
    _summarise_submessage(summarised, max_field_size)
    return header + (_bounded(summarised, budget) or _NOTHING_SET)


def _bounded(msg: message.Message, max_chars: int) -> str:
    """`msg` as text format within `max_chars`, with whatever did not fit named in a trailing marker.

    Unknown fields are the reason this is not just a slice of the rendering. No field walk reaches them —
    `ListFields` does not report them — so an unknown field arrives here whole, however large. Rendering them is
    given up before known content is, but only once it costs a large share of the budget: below that they stay,
    being the likeliest of all to carry the answer nobody asked for. That carrying works only for unknown fields of
    `msg` itself, which text format appends after the known ones; one nested inside a submessage renders where the
    submessage does, so its content is named by size rather than rendered.
    """
    whole = _text_format(msg, unknown=True)
    if len(whole) <= max_chars:
        return whole
    unknown = _unknown_bytes(msg)
    if not unknown:
        return _truncated(whole, max_chars, '')
    known = _text_format(msg, unknown=False)
    rendered_unknown = whole[len(known) :] if whole.startswith(known) else ''
    if rendered_unknown and len(rendered_unknown) <= max_chars // _UNKNOWN_SHARE:
        return _truncated(known, max_chars, rendered_unknown)
    return _truncated(known, max_chars, f'<unknown fields: {unknown} bytes, not rendered>\n')


def _truncated(body: str, max_chars: int, suffix: str) -> str:
    """`body` and `suffix` if they fit `max_chars`; else `body` cut to a block boundary, with what was cut named."""
    if len(body) + len(suffix) <= max_chars:
        return body + suffix
    room = max(max_chars - len(suffix) - _MARKER_ALLOWANCE, 0)
    kept, closing = _cut(body, room)
    return f'{kept}{closing}{suffix}<truncated: rendered {len(kept)} of {len(body)} chars>\n'


def _closing(depth: int) -> str:
    """The braces closing `depth` open blocks, each at the indent text format opened its block on."""
    return ''.join(f'{"  " * level}}}\n' for level in reversed(range(depth)))


def _cut(body: str, room: int) -> tuple[str, str]:
    """How much of `body` fits `room`, and the braces closing whatever blocks that cut leaves open.

    Cutting mid-line would split a token, and a cut that left a block open would put the marker inside it, where
    it reads as a field of that block and the unbalanced brace makes the block look empty. So the cut lands on a
    line boundary and the open blocks are closed explicitly. Closing them is what lets the head survive at any
    depth: a response whose content all sits inside one top-level submessage reaches depth 0 only at its final
    line, so a cut restricted to that depth would keep nothing at all. A block closed this way reads as one that
    ended where it was cut, and the marker is what says otherwise.

    Text format quotes every string value, so a trailing `{` is always structure, never data.
    """
    kept = 0
    closing = ''
    position = 0
    depth = 0
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        after = depth + 1 if stripped.endswith('{') else depth - 1 if stripped == '}' else depth
        candidate = _closing(max(after, 0))
        if position + len(line) + len(candidate) > room:
            break
        position += len(line)
        depth = after
        kept = position
        closing = candidate
    return body[:kept], closing


def _unknown_bytes(msg: message.Message) -> int:
    """The wire bytes of the fields this build's descriptors cannot name, at any depth of `msg`."""
    known = type(msg)()
    known.CopyFrom(msg)  # CopyFrom carries unknown fields over; DiscardUnknownFields is recursive
    known.DiscardUnknownFields()
    return msg.ByteSize() - known.ByteSize()


def _text_format(msg: message.Message, *, unknown: bool) -> str:
    """`msg` as text format, never raising on a payload it cannot parse.

    Unknown fields are printed when `unknown`: a service deployed ahead of this image sends fields the guest's
    committed descriptor does not know, and those are the likeliest of all to carry the unexpected answer.
    """
    try:
        return text_format.MessageToString(msg, as_utf8=True, print_unknown_fields=unknown)
    except message.DecodeError:
        # An `Any` payload that does not parse against the type its `type_url` names. An empty pool resolves
        # no type_url, so the payload prints as bytes rather than being re-parsed and taking the render down.
        return text_format.MessageToString(
            msg, as_utf8=True, print_unknown_fields=unknown, descriptor_pool=descriptor_pool.DescriptorPool()
        )


def _summarise(msg: message.Message, max_field_size: int) -> None:
    """Replace every oversized field of ``msg``, recursively, with a marker naming its size."""
    for field, value in msg.ListFields():
        if field.type in _MESSAGE_TYPES:
            _summarise_message_field(field, value, max_field_size)
        elif field.type in _TEXT_TYPES:
            _summarise_text_field(msg, field, value, max_field_size)


def _summarise_message_field(field: descriptor.FieldDescriptor, value: object, max_field_size: int) -> None:
    """Descend into a message field, whether it is singular, repeated, or a map."""
    if isinstance(value, message.Message):
        _summarise_submessage(value, max_field_size)
    elif _is_map(field):
        _summarise_map(field, cast('MutableMapping[object, object]', value), max_field_size)
    else:
        for entry in cast('Iterable[object]', value):
            if isinstance(entry, message.Message):
                _summarise_submessage(entry, max_field_size)


def _is_map(field: descriptor.FieldDescriptor) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def _summarise_map(
    field: descriptor.FieldDescriptor, container: MutableMapping[object, object], max_field_size: int
) -> None:
    """Summarise a map's entries in place — key and value alike are as capable of being oversized as any field."""
    values = _map_entry(field).fields_by_name['value']
    for key, entry in list(container.items()):
        if isinstance(entry, message.Message):
            _summarise_submessage(entry, max_field_size)
        elif isinstance(entry, str | bytes) and len(entry) > max_field_size:
            container[key] = _marker(values, entry)
    _reseat_long_keys(container)


def _reseat_long_keys(container: MutableMapping[object, object]) -> None:
    """Move every entry whose own key is oversized to a shortened key, so a key cannot flood the rendering.

    Every over-long key is lifted out before any shortened one goes back, because a key can be crafted to equal
    another's shortened form: reseating one at a time would overwrite it, dropping an entry, and which one went
    would depend on iteration order. Once they are all out, a shortened key — longer than the cap by construction —
    can collide with nothing that was left in place.
    """
    lifted: list[tuple[str, object]] = []
    for key in [key for key in container if isinstance(key, str) and len(key) > _MAX_SHORT_CHARS]:
        entry = container[key]
        if isinstance(entry, message.Message):
            held = type(entry)()
            held.CopyFrom(entry)  # the map's own message is freed by the delete below
            lifted.append((_short(key), held))
        else:
            lifted.append((_short(key), entry))
        del container[key]
    for key, entry in lifted:
        if isinstance(entry, message.Message):
            # A message-valued map admits no assignment; the auto-created entry is copied into instead.
            cast('message.Message', container[key]).CopyFrom(entry)
        else:
            container[key] = entry


def _short(text: str) -> str:
    """`text` unchanged, or cut to its head with its length and a digest — so two long ones stay distinct."""
    if len(text) <= _MAX_SHORT_CHARS:
        return text
    digest = hashlib.sha256(text.encode()).hexdigest()[:_SHORT_DIGEST_CHARS]
    return f'{text[:_MAX_SHORT_CHARS]}<elided: {len(text)} chars, {digest}>'


def _map_entry(field: descriptor.FieldDescriptor) -> descriptor.Descriptor:
    entry = field.message_type
    if entry is None:
        raise TypeError(f'{field.full_name} is a map field with no entry type')
    return entry


def _summarise_submessage(sub: message.Message, max_field_size: int) -> None:
    name = sub.DESCRIPTOR.full_name
    if name in _JSON_NAMES:
        if sub.ByteSize() > max_field_size:
            _elide_json(sub)
    elif name == _ANY_NAME:
        # Text format re-parses `value` against the type `type_url` names, so a marker written into the payload
        # is a decode error at render time. The whole `Any` goes instead, with the type it held named.
        if sub.ByteSize() > max_field_size:
            packed = cast('any_pb2.Any', sub)
            packed.type_url = f'<elided: {packed.ByteSize()} bytes of {_short(packed.type_url)}>'
            packed.ClearField('value')
    else:
        _summarise(sub, max_field_size)


def _summarise_text_field(
    msg: message.Message, field: descriptor.FieldDescriptor, value: object, max_field_size: int
) -> None:
    if isinstance(value, str | bytes):
        if len(value) > max_field_size:
            if field.is_extension:  # a proto2 extension is not reachable by name
                msg.Extensions[field] = _marker(field, value)  # type: ignore[index] — the descriptor IS the handle
            else:
                setattr(msg, field.name, _marker(field, value))
        return
    # Mutated in place: `ListFields` yields the live container, and an extension's is not reachable by name.
    elements = cast('MutableSequence[str | bytes]', value)
    for index, element in enumerate(elements):
        if len(element) > max_field_size:
            elements[index] = _marker(field, element)


def _marker(field: descriptor.FieldDescriptor, value: str | bytes) -> str | bytes:
    if field.type == descriptor.FieldDescriptor.TYPE_BYTES:
        return f'<elided: {len(value)} bytes>'.encode()
    return f'<elided: {len(value)} chars>'


def _elide_json(sub: message.Message) -> None:
    """Replace a JSON-valued field with a marker naming its size, keeping a `Struct`'s top-level keys."""
    size = sub.ByteSize()
    name = sub.DESCRIPTOR.full_name
    if name == _STRUCT_NAME:
        struct = cast('struct_pb2.Struct', sub)
        keys = sorted(struct.fields)  # a Struct's fields are a proto map, whose iteration order is arbitrary
        # Each listed key is cut too: one oversized key would otherwise make the marker the whole rendering.
        listed = ', '.join(_short(key) for key in keys[:_SUMMARISED_KEYS])
        rest = '' if len(keys) <= _SUMMARISED_KEYS else f', +{len(keys) - _SUMMARISED_KEYS} more'
        struct.Clear()
        struct[_ELIDED_KEY] = f'{size} bytes; {len(keys)} key(s): {listed}{rest}'
    elif name == _LIST_VALUE_NAME:
        values = cast('struct_pb2.ListValue', sub)
        items = len(values.values)
        values.Clear()
        values.append(f'<elided: {size} bytes, {items} items>')
    else:
        value = cast('struct_pb2.Value', sub)
        value.Clear()
        value.string_value = f'<elided: {size} bytes>'
