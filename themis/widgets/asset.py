"""The asset an ``::embed[<path>]`` names: where it may live, and what its bytes must be to be drawn.

An asset is a file whose bytes are a serialized ``google.protobuf.Any`` and nothing else, in the one encoding every
parser reads alike: the type URL, then the value, and no other field. It draws when the type URL's tail names a
message the descriptor pool holds and the ``widget`` option marks, the payload bytes parse as that message, and the
message passes its protovalidate rules. A path is admitted over a character set markdown has
no reading of that differs from the raw string, so the browser, which reads the directive through a different
parser, resolves the same path the linter checked.

``encode`` is how a payload becomes an asset and ``decode`` how an asset is read back; each raises `AssetError`
naming what is wrong, for the producer to fix.
"""

from __future__ import annotations

import re
import typing

import protovalidate
from google.protobuf import any_pb2, descriptor, descriptor_pool, message, message_factory, unknown_fields

from buf.validate import validate_pb2
from themis.widgets.models import (
    checklist_pb2,  # noqa: F401  (registers the payload in the descriptor pool `decode` reads)
    widget_pb2,
)

_PATH = re.compile(r'[A-Za-z0-9._/-]+')
_GIT_DIR = '.git'


# The browser draws an Any only in this encoding, which every parser reads as the same type URL and value: one that
# parses differently in two parsers could draw there a version `parse` reads as no asset at all.
_NOT_CANONICAL = (
    'the file is a google.protobuf.Any in an encoding other than the one `encode` writes: the type URL, then the '
    'value, and no other field'
)


class AssetError(ValueError):
    """An asset, or the path naming it, that the working document cannot draw; the message says why."""


def check_path(path: str) -> None:
    """Raise unless ``path`` is one an ``::embed`` may name: relative, segment by segment, over the character set.

    Raises:
        AssetError: If the path is empty or absolute, has a character outside ``A-Z a-z 0-9 - _ . /``, or has a
            segment that is empty, ``.``, ``..`` or ``.git``.
    """
    if not path:
        raise AssetError('the path is empty')
    if not _PATH.fullmatch(path):
        raise AssetError(f'{path!r} has a character outside A-Z, a-z, 0-9, "-", "_", "." and "/"')
    if path.startswith('/'):
        raise AssetError(f'{path!r} is absolute; name the file relative to the document')
    for segment in path.split('/'):
        if segment in ('', '.', '..'):
            raise AssetError(f'{path!r} has an empty, "." or ".." segment')
        if segment.lower() == _GIT_DIR:
            raise AssetError(f'{path!r} names a path inside .git')


def is_payload_type(desc: descriptor.Descriptor) -> bool:
    """Whether the ``widget`` option marks ``desc`` as a payload type."""
    # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
    return desc.GetOptions().Extensions[widget_pb2.widget]  # pyright: ignore[reportArgumentType]


def encode(payload: message.Message) -> bytes:
    """The asset bytes for ``payload``: the message, validated and wrapped in an ``Any``.

    Raises:
        AssetError: If the message's type is not a marked payload type, or it fails its rules.
    """
    _payload_type(payload.DESCRIPTOR.full_name)
    _validate(payload)
    wrapped = any_pb2.Any()
    wrapped.Pack(payload)
    return wrapped.SerializeToString()


def decode(data: bytes) -> message.Message:
    """The payload an asset carries, parsed as its type and validated.

    Raises:
        AssetError: If the bytes are not a serialized ``Any`` in the encoding `encode` writes, it names no type or
            a type the pool does not hold or the option does not mark, the payload does not parse as that type, or it
            fails its rules.
    """
    wrapped = any_pb2.Any()
    try:
        wrapped.ParseFromString(data)
    except message.DecodeError as error:
        raise AssetError('the file is not a serialized google.protobuf.Any') from error
    if not wrapped.type_url:
        raise AssetError('the file is not a serialized google.protobuf.Any naming a type')
    if len(unknown_fields.UnknownFieldSet(wrapped)) or wrapped.SerializeToString() != data:
        raise AssetError(_NOT_CANONICAL)
    name = wrapped.TypeName()
    desc = _payload_type(name)
    payload = message_factory.GetMessageClass(desc)()
    try:
        payload.ParseFromString(wrapped.value)
    except message.DecodeError as error:
        raise AssetError(f'the payload does not parse as {name}') from error
    _require_wire_types(payload, name)
    _validate(payload)
    return payload


# The package every widget payload is declared in: an asset naming a type in it that this build does not hold was
# written by another build, and nothing here can say which of its fields are a user's judgements.
WIDGET_PACKAGE = 'themis.widgets.'


def parse(data: bytes) -> message.Message | None:
    """The payload a file carries if it is a widget asset, parsed as its type without its validation rules.

    What the ownership rule reads: whether a version passes its rules does not change who wrote its fields, so a
    version that fails them is compared all the same, and so is one in another encoding than `encode`'s. A file
    that is no widget asset, or wraps a type outside the widgets package, is None; one wrapping a type inside it is
    a widget asset or refused, since another build may mark a type this one does not. A version this refuses cannot
    be compared, so a push rewriting it as an asset is refused too; removing it, or writing a file that is no asset
    over it, lands; a file this parser does not
    read as an ``Any`` at all is one the browser does not draw either, since it draws only `encode`'s encoding.

    Raises:
        AssetError: If the file names a type in the widgets package that this build does not hold or does not mark,
            or a payload type whose bytes do not parse or carry a known field in a wire type other than its own.
    """
    wrapped = any_pb2.Any()
    try:
        wrapped.ParseFromString(data)
    except message.DecodeError:
        return None
    if not wrapped.type_url:
        return None
    name = wrapped.TypeName()
    try:
        desc = descriptor_pool.Default().FindMessageTypeByName(name)
    except KeyError as error:
        if name.startswith(WIDGET_PACKAGE):
            raise AssetError(
                f'{name} is a widget type this build does not know, so its fields cannot be checked'
            ) from error
        return None
    if not is_payload_type(desc):
        if name.startswith(WIDGET_PACKAGE):
            raise AssetError(
                f'{name} is in the widgets package and no widget payload type this build knows, so its fields '
                'cannot be checked'
            )
        return None
    payload = message_factory.GetMessageClass(desc)()
    try:
        payload.ParseFromString(wrapped.value)
    except message.DecodeError as error:
        raise AssetError(f'the payload does not parse as {name}, so its fields cannot be checked') from error
    _require_wire_types(payload, name)
    return payload


_FD = descriptor.FieldDescriptor
_MESSAGE_TYPES = frozenset({_FD.TYPE_MESSAGE, _FD.TYPE_GROUP})
_VARINT, _I64, _LEN, _START_GROUP, _I32 = 0, 1, 2, 3, 5
_I64_TYPES = frozenset({_FD.TYPE_FIXED64, _FD.TYPE_SFIXED64, _FD.TYPE_DOUBLE})
_I32_TYPES = frozenset({_FD.TYPE_FIXED32, _FD.TYPE_SFIXED32, _FD.TYPE_FLOAT})
_LEN_TYPES = frozenset({_FD.TYPE_STRING, _FD.TYPE_BYTES, _FD.TYPE_MESSAGE})


def _wire_types(field: descriptor.FieldDescriptor) -> frozenset[int]:
    """The wire types a value of `field` may be written in: its own, and length-delimited for a packable list."""
    if field.type == _FD.TYPE_GROUP:
        return frozenset({_START_GROUP})
    if field.type in _LEN_TYPES:
        return frozenset({_LEN})
    own = _I64 if field.type in _I64_TYPES else _I32 if field.type in _I32_TYPES else _VARINT
    return frozenset({own, _LEN}) if field.is_repeated else frozenset({own})


def _is_map(field: descriptor.FieldDescriptor) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def _require_wire_types(msg: message.Message, where: str) -> None:
    """Raise unless every field `msg` and each message in it carry has the wire type its schema gives it.

    upb keeps a known field written in another wire type as a field it does not know, where protobuf-es, the
    browser's parser, reads it as the field regardless: the two would read one asset as two, a tick set in one and
    not in the other.

    Raises:
        AssetError: If a message carries a field this build knows in a wire type other than its own, or a map entry
            that does not read as a key and a value.
    """
    # The stubs type a message's DESCRIPTOR as the pure and the upb class together; upb's is the one in use.
    known = {field.number: field for field in typing.cast('descriptor.Descriptor', msg.DESCRIPTOR).fields}
    for unknown in unknown_fields.UnknownFieldSet(msg):
        field = known.get(unknown.field_number)
        if field is None:
            continue
        # upb keeps an entry it does not read as a key and a value whole, under the map's own number: one holding a
        # field in another wire type, or a field besides the two.
        if _is_map(field):
            raise AssetError(
                f'{where} carries an entry of its map {field.name} that does not read as a key and a value, which '
                'parsers read differently'
            )
        # A closed enum's value this build does not know is kept under its own number and wire type, alike in both.
        if unknown.wire_type not in _wire_types(field):
            raise AssetError(
                f'{where} carries its field {field.name} in a wire type its schema does not give it, which parsers '
                'read differently'
            )
    for field, value in msg.ListFields():
        if field.type not in _MESSAGE_TYPES:
            continue
        inner = f'{where}.{field.name}'
        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            values = value.values() if field.message_type.fields_by_name['value'].type in _MESSAGE_TYPES else ()
            for each in values:
                _require_wire_types(each, inner)
        elif field.is_repeated:
            for each in value:
                _require_wire_types(each, inner)
        else:
            _require_wire_types(value, inner)


def _payload_type(name: str) -> descriptor.Descriptor:
    """The marked message named ``name`` in the default descriptor pool, the one ``decode`` reads types from.

    Raises:
        AssetError: If the pool holds no such message, or the option does not mark it.
    """
    try:
        desc = descriptor_pool.Default().FindMessageTypeByName(name)
    except KeyError as error:
        raise AssetError(f'{name} is not a message type this build knows') from error
    if not is_payload_type(desc):
        raise AssetError(f'{name} is not a widget payload type')
    return desc


def _validate(payload: message.Message) -> None:
    try:
        protovalidate.validate(payload)
    except protovalidate.ValidationError as error:
        violations = '; '.join(
            f'{_field_path(violation.proto.field)}: {violation.proto.message}' for violation in error.violations
        )
        raise AssetError(f'the {payload.DESCRIPTOR.full_name} payload fails its rules: {violations}') from error


def _field_path(path: validate_pb2.FieldPath) -> str:
    """A violation's field path as ``items[2].id``."""
    parts = []
    for element in path.elements:
        index = f'[{element.index}]' if element.HasField('index') else ''
        parts.append(f'{element.field_name}{index}')
    return '.'.join(parts) or '<message>'
