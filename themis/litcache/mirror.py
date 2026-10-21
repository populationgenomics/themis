"""Load an index's JSON work record into its mirror proto, and serialise the record as text.

A mirror is hand-authored against the JSON an index publishes, so it lags the index; it is the
record's interim form, kept beside the text until readers of the text exist
(docs/design/litcache-manifest.md, "The bibliographic record"). The load drops a key the mirror
does not declare, at any depth, so an index adding a key fails no paper. The record's text, stored
beside the mirror, keeps every key the index served. A value of a shape a declared field cannot hold
still fails the load, as drift in a key the mirror declares. A key spelled as a field's proto name
rather than its `json_name` loads into that field, as proto3-JSON specifies; a record carrying both
spellings of a singular scalar fails the load, and of any other field keeps the last in the mirror.
"""

from __future__ import annotations

import dataclasses
import json
import math
import typing
from collections.abc import Callable, Mapping

from google.protobuf import descriptor, json_format, message

Wrap = Callable[[Mapping[str, object]], dict[str, object]]


class SchemaDriftError(Exception):
    """A record holds a value its mirror or its stored text cannot hold.

    Either a key the mirror declares holds a value of a shape its field cannot hold, or the record
    holds a number standard JSON text cannot carry. The remedy is the mirror's field (or, for a
    number, the index's record); until then the paper is dead-lettered with `detail`, which names
    the key.
    """

    def __init__(self, index: str, detail: str) -> None:
        super().__init__(f'{index} record cannot be stored: {detail}')
        self.index = index
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class Loaded[M: message.Message]:
    """An index's work record in the two forms `metadata.pb` stores it in.

    Attributes:
        work: The record loaded into its mirror, every key the mirror does not declare dropped.
        text: The record as JSON text (`to_text`), every key kept.
    """

    work: M
    text: str


def load[M: message.Message](record: Mapping[str, object], target: M, *, index: str, wrap: Wrap) -> Loaded[M]:
    """Load a work record into its mirror and serialise it as text.

    Args:
        record: The work as the index served it.
        target: An empty message of the mirror's record type.
        index: The index's name, for the error.
        wrap: The loader's wrapping of the shapes the mirror's header names (an array of arrays, an
            object whose values are arrays); applied to the mirror's copy only, never to the text.

    Returns:
        The record in both forms; `work` is `target`, populated.

    Raises:
        SchemaDriftError: If a key the mirror declares holds a value of a shape its field cannot
            hold, or the record holds a NaN or infinite number.
    """
    try:
        # The stubs type DESCRIPTOR as a union with upb's Descriptor, which has the same API.
        desc = typing.cast('descriptor.Descriptor', target.DESCRIPTOR)
        declared = _declared_only(wrap(record), desc, '')
        work = json_format.ParseDict(declared, target)
    except _NotAnObjectError as e:
        raise SchemaDriftError(index, str(e)) from e
    except json_format.ParseError as e:
        # The parser's first line names the key; the rest lists every field the message declares.
        raise SchemaDriftError(index, str(e).splitlines()[0]) from e
    try:
        text = to_text(record)
    except ValueError as e:
        raise SchemaDriftError(index, str(e)) from e
    return Loaded(work=work, text=text)


class _NotAnObjectError(ValueError):
    pass


def _declared_only(value: object, desc: descriptor.Descriptor, path: str) -> dict[str, object]:
    """Copy an object, keeping only the keys `desc` declares, at every depth.

    Dropped here rather than by the parser's `ignore_unknown_fields`, which reads a string or a list
    given for a message field as an object of unknown keys and loads an empty message.

    Raises:
        _NotAnObjectError: If `value`, or a value a declared message field holds, is not an object.
    """
    if not isinstance(value, Mapping):
        raise _NotAnObjectError(f'{path or "the record"} is {type(value).__name__}, where an object is declared')
    fields = {f.json_name: f for f in desc.fields} | {f.name: f for f in desc.fields}
    kept: dict[str, object] = {}
    for key, item in value.items():
        field = fields.get(str(key))
        if field is None:
            continue
        kept[key] = _declared_value(item, field, f'{path}.{key}' if path else str(key))
    return kept


def _declared_value(item: object, field: descriptor.FieldDescriptor, path: str) -> object:
    entry = field.message_type
    if entry is None or item is None:
        return item
    if entry.GetOptions().map_entry:
        values = entry.fields_by_name['value'].message_type
        if values is None or not isinstance(item, Mapping):
            return item  # the parser checks a map of scalars, and that a map is an object
        return {k: None if v is None else _declared_only(v, values, f'{path}.{k}') for k, v in item.items()}
    if field.is_repeated:
        if not isinstance(item, list):
            return item  # the parser refuses a repeated field that is not an array
        return [None if v is None else _declared_only(v, entry, f'{path}[{i}]') for i, v in enumerate(item)]
    return _declared_only(item, entry, path)


def to_text(record: Mapping[str, object]) -> str:
    """Serialise a work record to the text `metadata.pb` stores: every key, in order, compact.

    The text is ASCII, with every other character escaped (a lone surrogate included), and parses
    back to a value equal to `record`.

    Raises:
        ValueError: If `record` holds a NaN or infinite number, which standard JSON text cannot
            carry, naming its path.
    """
    unstorable = _non_finite(record, '')
    if unstorable is not None:
        raise ValueError(unstorable)
    return json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def parse_text(text: str) -> dict[str, object]:
    """Parse stored text back into the work record it was serialised from.

    Raises:
        ValueError: If `text` is not standard JSON (`json.JSONDecodeError`, or a `NaN` or `Infinity`
            token, which Python's parser accepts and `to_text` never writes), or is JSON but not an
            object.
    """
    record = json.loads(text, parse_constant=_refuse_constant)
    if not isinstance(record, dict):
        raise ValueError(f'expected a JSON object, got {type(record).__name__}')
    return record


def _refuse_constant(token: str) -> object:
    raise ValueError(f'{token} is not standard JSON')


def _non_finite(value: object, path: str) -> str | None:
    """Describe the first NaN or infinite number under `value`, or `None`."""
    match value:
        case float() if not math.isfinite(value):
            return f'{path or "the record"} is {value!r}, which standard JSON cannot carry'
        case Mapping():
            for key, item in value.items():
                found = _non_finite(item, f'{path}.{key}' if path else str(key))
                if found is not None:
                    return found
        case list():
            for position, item in enumerate(value):
                found = _non_finite(item, f'{path}[{position}]')
                if found is not None:
                    return found
    return None
