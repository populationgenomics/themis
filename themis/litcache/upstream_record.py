"""An index's JSON work record held as text, and what the readers of it share.

Crossref's and OpenAlex's records are stored in `metadata.pb` as JSON text, the value each index
served re-serialised with nothing dropped or renamed, and read through readers of only the fields
Themis uses (docs/design/proto.md, "Upstream JSON held as text"): the identifiers at ingest, beside
each index's client, and the metadata fields in `themis.litcache.metadata_fields`. This module holds
what they share: the text form, the error a reader raises, and `JsonObject`, which reads a field to a
type or raises `SchemaDriftError` naming its path.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import re
from collections.abc import Callable, Mapping


class Index(enum.StrEnum):
    """An index whose work record `metadata.pb` holds: PubMed's typed, the others' as JSON text."""

    PUBMED = 'pubmed'
    CROSSREF = 'crossref'
    OPENALEX = 'openalex'


class SchemaDriftError(Exception):
    """A work an index served holds a value Themis cannot read or store as it reads and stores it.

    A field a reader reads is missing or has a shape the reader cannot read, or the work holds a
    number standard JSON text cannot carry. Every other key of the record is ignored, so the remedy
    is the reader. Until then the paper is dead-lettered with this message, which names the index
    and the field's path.

    Attributes:
        index: The index whose record drifted.
        detail: The drifted field's path and what is wrong with it.
    """

    def __init__(self, index: Index, detail: str) -> None:
        super().__init__(f'schema drift in the {index} record: {detail}')
        self.index = index
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class Record[F]:
    """An index's work record: its stored text, and the fields a reader read from it.

    Attributes:
        text: The work as JSON text (`to_text`), for the source record's field.
        fields: What the reader returned for the same work.
    """

    text: str
    fields: F


def load[F](index: Index, work: Mapping[str, object], reader: Callable[[Mapping[str, object]], F]) -> Record[F]:
    """Read a parsed work with a reader, and serialise it for storage.

    Raises:
        SchemaDriftError: If the reader cannot read a field it needs, or the work holds a number
            standard JSON text cannot carry (`to_text`).
    """
    fields = reader(work)
    try:
        text = to_text(work)
    except ValueError as e:
        raise SchemaDriftError(index, str(e)) from e
    return Record(text=text, fields=fields)


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


def json_type(value: object) -> str:
    """The JSON type of a parsed value, worded for a message (`'an object'`, `'null'`)."""
    match value:
        case None:
            return 'null'
        case bool():
            return 'a boolean'
        case str():
            return 'a string'
        case int() | float():
            return 'a number'
        case list():
            return 'an array'
        case Mapping():
            return 'an object'
        case _:
            return type(value).__name__


@dataclasses.dataclass(frozen=True)
class JsonObject:
    """One JSON object within an index's record, read field by field.

    Each accessor names the contract it reads under: `required_*` needs the key present,
    `optional_*` reads an absent key as `None`, and `*_or_null` admits a JSON null, read as `None`.
    A string read must hold no lone surrogate, as a proto `string` cannot. A field outside its
    contract raises `SchemaDriftError` naming its path from the record's root.
    """

    index: Index
    path: str
    value: Mapping[str, object]

    @classmethod
    def root(cls, index: Index, work: Mapping[str, object]) -> JsonObject:
        """The work object itself, whose fields' paths are their bare keys."""
        return cls(index=index, path='', value=work)

    def required_str(self, key: str) -> str:
        value = self._required(key)
        if not isinstance(value, str):
            raise self._drift(key, value, 'a string')
        return self._text(key, value)

    def required_str_or_null(self, key: str) -> str | None:
        value = self._required(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise self._drift(key, value, 'a string or null')
        return self._text(key, value)

    def optional_str(self, key: str) -> str | None:
        if key not in self.value:
            return None
        return self.required_str(key)

    def optional_str_or_null(self, key: str) -> str | None:
        if key not in self.value:
            return None
        return self.required_str_or_null(key)

    def optional_str_matching(self, key: str, pattern: re.Pattern[str], expected: str) -> re.Match[str] | None:
        """An optional string field that, when stated, must match `pattern` whole.

        Args:
            key: The field's key.
            pattern: The form the field's value is read in.
            expected: That form in words, for the drift message.
        """
        value = self.optional_str_or_null(key)
        if value is None:
            return None
        match = pattern.fullmatch(value)
        if match is None:
            raise SchemaDriftError(self.index, f'{self._path(key)} is {value!r}, where {expected} is read')
        return match

    def required_bool(self, key: str) -> bool:
        value = self._required(key)
        if not isinstance(value, bool):
            raise self._drift(key, value, 'a boolean')
        return value

    def optional_int_or_null(self, key: str) -> int | None:
        value = self.value.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._drift(key, value, 'an integer or null')
        return value

    def optional_str_list(self, key: str) -> list[str] | None:
        if key not in self.value:
            return None
        value = self.value[key]
        if not isinstance(value, list):
            raise self._drift(key, value, 'an array of strings')
        for position, item in enumerate(value):
            if not isinstance(item, str):
                raise self._drift(f'{key}[{position}]', item, 'a string')
            self._text(f'{key}[{position}]', item)
        return value

    def optional_object_list(self, key: str) -> list[JsonObject] | None:
        if key not in self.value:
            return None
        value = self.value[key]
        if not isinstance(value, list):
            raise self._drift(key, value, 'an array of objects')
        objects = []
        for position, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise self._drift(f'{key}[{position}]', item, 'an object')
            objects.append(JsonObject(index=self.index, path=self._path(f'{key}[{position}]'), value=item))
        return objects

    def optional_object_list_or_null(self, key: str) -> list[JsonObject] | None:
        if self.value.get(key) is None:
            return None
        return self.optional_object_list(key)

    def optional_list(self, key: str) -> list[object] | None:
        if key not in self.value:
            return None
        value = self.value[key]
        if not isinstance(value, list):
            raise self._drift(key, value, 'an array')
        return value

    def checked_keys(self) -> list[str]:
        """The object's keys, each checked as text a proto `string` can hold."""
        return [self._text(key, key) for key in self.value]

    def required_object(self, key: str) -> JsonObject:
        value = self._required(key)
        if not isinstance(value, Mapping):
            raise self._drift(key, value, 'an object')
        return JsonObject(index=self.index, path=self._path(key), value=value)

    def required_object_or_null(self, key: str) -> JsonObject | None:
        value = self._required(key)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise self._drift(key, value, 'an object or null')
        return JsonObject(index=self.index, path=self._path(key), value=value)

    def optional_object_or_null(self, key: str) -> JsonObject | None:
        if key not in self.value:
            return None
        return self.required_object_or_null(key)

    def drift(self, key: str, detail: str) -> SchemaDriftError:
        """A drift error for a field of this object whose value breaks a rule its reader states."""
        return SchemaDriftError(self.index, f'{self._path(key)} {detail}')

    def _path(self, key: str) -> str:
        return f'{self.path}.{key}' if self.path else key

    def _text(self, key: str, value: str) -> str:
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise SchemaDriftError(self.index, f'{self._path(key)} holds a lone surrogate') from None
        return value

    def _required(self, key: str) -> object:
        if key not in self.value:
            raise SchemaDriftError(self.index, f'{self._path(key)} is missing')
        return self.value[key]

    def _drift(self, key: str, value: object, expected: str) -> SchemaDriftError:
        return SchemaDriftError(self.index, f'{self._path(key)} is {json_type(value)}, where {expected} is read')
