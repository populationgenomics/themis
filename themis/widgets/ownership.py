"""A user's judgements in a widget payload, and the rule an agent's change to an asset keeps.

A field the ``guard`` option marks is a user's judgement on the other fields of its message; its default means no
judgement. What a guard judges, its protected content, is every field of its message and everything beneath them,
except the message's ``element_key``, the fields the guard ``ignores``, and every guard at any depth, so one judgement
never clears another. For any change the agent makes to an asset:

- a new element's guards are at their default: the agent never makes a judgement;
- on an element present before and after, matched by its ``element_key``, a guard whose protected content is
  unchanged is unchanged, and one whose protected content changed is at its default;
- an element may be removed, and one removed and added again, under any key, starts with its guards at their default.

The rule is read off the descriptors, so it holds for every marked payload. The elements of a repeated message are
matched by key, and reordering a list is free: a keyed list is protected content of the guards above it as the set of
its elements, whatever their order. The key is outside the protected content only of an element matched by it; the
root and a singular message judge their key like any other field. Every message the comparison visits has its unknown
fields held unchanged too, since the rule cannot tell what a field this build does not know is: a new element may carry
none. Two values are the same when their wire encodings are, which settles NaN and negative zero the way the bytes do.
`check_schema` refuses a schema the rule cannot read.

`violations` checks a change; `carry` builds the agent's new version with each judgement kept where what it judges is
unchanged, and reports each one the change clears, which is how the guest's helper writes one. The cases in
apps/web/src/widgets/ownership-cases.test-support.json hold the rule, and hold this schema check and the browser's
(apps/web/src/widgets/ownership.ts) to the same schemas.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import typing
from collections.abc import Sequence

from google.protobuf import descriptor, message

from themis.widgets.models import widget_pb2

_FD = descriptor.FieldDescriptor
_MESSAGE_TYPES = frozenset({_FD.TYPE_MESSAGE, _FD.TYPE_GROUP})
# What an element may be matched by: values whose identity is their text, number or truth. A float key would match
# NaN to nothing and 0.0 to -0.0, and an enum or bytes key is a shape no payload has needed.
_KEY_TYPES = frozenset(
    {
        _FD.TYPE_STRING,
        _FD.TYPE_BOOL,
        _FD.TYPE_INT32,
        _FD.TYPE_INT64,
        _FD.TYPE_UINT32,
        _FD.TYPE_UINT64,
        _FD.TYPE_SINT32,
        _FD.TYPE_SINT64,
        _FD.TYPE_FIXED32,
        _FD.TYPE_FIXED64,
        _FD.TYPE_SFIXED32,
        _FD.TYPE_SFIXED64,
    }
)
# How a change to a message's fields this build does not know is named among the fields a change altered.
UNKNOWN_FIELDS = 'fields this build does not know'


class SchemaError(ValueError):
    """A payload schema whose judgements the rule cannot read; the message names the field and the rule it breaks."""


@dataclasses.dataclass(frozen=True)
class Violation:
    """One change the agent may not make: where it is, as ``items[ps3].checked``, and what was done there.

    `changed` names the protected fields whose change clears the guard, for a guard the change leaves set.
    """

    path: str
    reason: str
    changed: tuple[str, ...] = ()

    @typing.override
    def __str__(self) -> str:
        return f'{self.path} {self.reason}'


SET_ON_NEW = "is a user's judgement, and the agent sets it on an element the user has not seen"
SET = "is a user's judgement, and the agent sets it where the user made none"
NOT_CLEARED = "is a user's judgement on what the agent's change alters, and the change leaves it set"
CHANGED = "is a user's judgement, and the agent's change alters it though nothing it judges changed"
UNKNOWN_CHANGED = "carries fields this build does not know, which the agent's change alters"
DUPLICATE_KEY = 'shares its key with another element'


@dataclasses.dataclass(frozen=True)
class Cleared:
    """A judgement `carry` cleared: since the fields `changed` names, which it judges, changed, or for `because`."""

    path: str
    changed: tuple[str, ...] = ()
    because: str = ''

    def __post_init__(self) -> None:
        if bool(self.changed) == bool(self.because):
            raise ValueError(f'a judgement cleared at {self.path} says either which fields changed or why, not both')

    @typing.override
    def __str__(self) -> str:
        if self.because:
            return f'{self.path} cleared: {self.because}'
        return f'{self.path} cleared: its {_listed(self.changed)} changed; the user reviews it again'


REMOVED = 'its element was removed'
SHARED_KEY = 'its element shares its key with another, so neither is carried across'
RETYPED = 'the asset is now another payload type'


def judgements(msg: message.Message, path: str = '') -> list[str]:
    """Where `msg` holds a judgement, as ``items[ps3].checked``: every guard in it or beneath it not at its default.

    An element sharing its key with another in its list is named with its ordinal among them, ``items[a#2]``.
    """
    found: list[str] = []
    desc = _descriptor(msg)
    for field in desc.fields:
        where = _join(path, field.name)
        if is_guard(field):
            if _encoding(msg, field):
                found.append(where)
        elif _holds_beneath(field):
            if _repeated(field):
                elements = getattr(msg, field.name)
                for element, at in zip(elements, _element_paths(elements, field, where), strict=True):
                    found.extend(judgements(element, at))
            elif msg.HasField(field.name):
                found.extend(judgements(getattr(msg, field.name), where))
    return found


def cleared_all(msg: message.Message, because: str, path: str = '') -> list[Cleared]:
    """Each judgement `msg` holds, as cleared `because`: what dropping `msg` whole does to them."""
    return [Cleared(where, because=because) for where in judgements(msg, path)]


def guard_of(field: descriptor.FieldDescriptor) -> widget_pb2.Guard | None:
    """The ``guard`` option on `field`, or None when it is not a guard."""
    options = field.GetOptions()
    # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
    if not options.HasExtension(widget_pb2.guard):  # pyright: ignore[reportArgumentType]
        return None
    return options.Extensions[widget_pb2.guard]  # pyright: ignore[reportArgumentType]


def is_guard(field: descriptor.FieldDescriptor) -> bool:
    """Whether the ``guard`` option marks `field` as a user's judgement."""
    return guard_of(field) is not None


def is_element_key(field: descriptor.FieldDescriptor) -> bool:
    """Whether the ``element_key`` option marks `field` as what its message's elements are matched by."""
    return bool(field.GetOptions().Extensions[widget_pb2.element_key])  # pyright: ignore[reportArgumentType]


def _is_message(field: descriptor.FieldDescriptor) -> bool:
    return field.type in _MESSAGE_TYPES


def _message_type(field: descriptor.FieldDescriptor) -> descriptor.Descriptor:
    if field.message_type is None:
        raise ValueError(f'{field.full_name} is not a message field')
    return field.message_type


def _descriptor(msg: message.Message) -> descriptor.Descriptor:
    # The stubs type a message's DESCRIPTOR as the pure and the upb class together; upb's is the one in use.
    return typing.cast('descriptor.Descriptor', msg.DESCRIPTOR)


def _is_map(field: descriptor.FieldDescriptor) -> bool:
    return _is_message(field) and _message_type(field).GetOptions().map_entry


def _repeated(field: descriptor.FieldDescriptor) -> bool:
    return field.is_repeated


def _holds_beneath(field: descriptor.FieldDescriptor) -> bool:
    """Whether `field` is a message field, not itself a guard, whose message holds a guard: one the rule descends."""
    return _is_message(field) and not is_guard(field) and holds_guards(_message_type(field))


def holds_guards(desc: descriptor.Descriptor) -> bool:
    """Whether `desc`, or any message beneath it, declares a guard."""
    return _holds(desc)


@functools.cache
def _holds(desc: descriptor.Descriptor, seen: frozenset[str] = frozenset()) -> bool:
    if desc.full_name in seen:
        return False
    inner = seen | {desc.full_name}
    return any(is_guard(field) or (_is_message(field) and _holds(_message_type(field), inner)) for field in desc.fields)


def key_field(desc: descriptor.Descriptor) -> descriptor.FieldDescriptor:
    """The field the elements of a list of `desc` are matched by; `check_schema` has vouched there is one."""
    [key] = [field for field in desc.fields if is_element_key(field)]
    return key


def check_schema(desc: descriptor.Descriptor) -> None:
    """Raise unless the rule can read `desc` and every message beneath it.

    Raises:
        SchemaError: If a guard is a map, shares a oneof with a field that is not a guard, or ignores a name that is
            not a field of its message, is the message's element key or a guard, or is listed twice; if a map's
            values hold guards; or if a list's elements hold them without marking exactly one key: a singular string,
            integer or bool field outside any oneof, and not a guard; or if any field beneath `desc` is of a closed
            enum.
    """
    _check(desc)
    _check_enums(desc)


@functools.cache
def _check_enums(desc: descriptor.Descriptor, seen: frozenset[str] = frozenset()) -> None:
    if desc.full_name in seen:
        return
    inner = seen | {desc.full_name}
    for field in desc.fields:
        enum = field.enum_type
        if enum is not None and enum.is_closed:
            raise SchemaError(
                f'{field.full_name} is of the closed enum {enum.full_name}, which keeps a value a build does not know '
                "apart from its field; a payload's enums have to be open"
            )
        if _is_message(field):
            _check_enums(_message_type(field), inner)


@functools.cache
def _check(desc: descriptor.Descriptor, seen: frozenset[str] = frozenset()) -> None:
    if desc.full_name in seen:
        return
    inner = seen | {desc.full_name}
    for field in desc.fields:
        guard = guard_of(field)
        if guard is not None:
            _check_guard(desc, field, guard)
        elif _holds_beneath(field):
            element = _message_type(field)
            if _is_map(field):
                raise SchemaError(f'{field.full_name} is a map whose values hold guards, which the rule does not read')
            if _repeated(field):
                _check_key(field, element)
            _check(element, inner)


def _check_guard(desc: descriptor.Descriptor, field: descriptor.FieldDescriptor, guard: widget_pb2.Guard) -> None:
    if _is_map(field):
        raise SchemaError(f'{field.full_name} is a map, and a map may not be a guard')
    oneof = field.containing_oneof
    if oneof is not None and not all(is_guard(member) for member in oneof.fields):
        raise SchemaError(f'{field.full_name} is a guard sharing the oneof {oneof.name} with a field that is not one')
    for name in guard.ignores:
        ignored = desc.fields_by_name.get(name)
        if ignored is None:
            raise SchemaError(f'{field.full_name} ignores {name}, which is not a field of {desc.full_name}')
        if is_element_key(ignored):
            raise SchemaError(f'{field.full_name} ignores {name}, the element key, which no guard judges anyway')
        if is_guard(ignored):
            raise SchemaError(f'{field.full_name} ignores {name}, a guard, which no guard judges anyway')
        if list(guard.ignores).count(name) > 1:
            raise SchemaError(f'{field.full_name} ignores {name} more than once')


def _check_key(field: descriptor.FieldDescriptor, element: descriptor.Descriptor) -> None:
    keys = [candidate for candidate in element.fields if is_element_key(candidate)]
    if len(keys) != 1:
        raise SchemaError(
            f'{field.full_name} lists {element.full_name} elements holding guards, so '
            f'{element.full_name} has to mark exactly one field element_key; it marks {len(keys)}'
        )
    [key] = keys
    if key.type not in _KEY_TYPES or _repeated(key) or is_guard(key) or key.containing_oneof is not None:
        raise SchemaError(
            f'{key.full_name} is a key, and has to be a singular string, integer or bool field outside any oneof, '
            'and not a guard'
        )


def same_payload(old: message.Message | None, new: message.Message) -> message.Message | None:
    """`old` if it is a payload of `new`'s type, else None: an asset that changed type was removed and added anew."""
    return old if old is not None and old.DESCRIPTOR.full_name == new.DESCRIPTOR.full_name else None


def violations(old: message.Message | None, new: message.Message) -> list[Violation]:
    """What the agent's change from `old` to `new` does to the user's judgements; empty when it keeps the rule.

    Args:
        old: The asset's payload before the change, or None when there was none of this type.
        new: The payload the agent writes.

    Raises:
        SchemaError: If the payload's schema is one the rule cannot read.
        ValueError: If `old` and `new` are messages of different types.
    """
    if old is not None and old.DESCRIPTOR.full_name != new.DESCRIPTOR.full_name:
        raise ValueError(f'{old.DESCRIPTOR.full_name} and {new.DESCRIPTOR.full_name} are different payloads')
    check_schema(_descriptor(new))
    found: list[Violation] = []
    _compare(old, new, '', found)
    return found


def _join(path: str, name: str) -> str:
    return f'{path}.{name}' if path else name


def _unknown(msg: message.Message) -> bytes:
    """The fields of `msg` this build does not know, as the wire carries them."""
    # Compared as bytes: a group's parsed form is an UnknownFieldSet, which compares by identity.
    rest = type(msg)()
    rest.CopyFrom(msg)
    for field in _descriptor(msg).fields:
        rest.ClearField(field.name)
    return rest.SerializeToString()


def _key_text(value: object) -> str:
    return ('true' if value else 'false') if isinstance(value, bool) else str(value)


def _element_paths(elements: Sequence[message.Message], field: descriptor.FieldDescriptor, path: str) -> list[str]:
    """Each element's path in the keyed list `field`, as ``items[ps3]``, in order.

    An element sharing its key with another is named with its ordinal among them, ``items[a#2]``.
    """
    key = key_field(_message_type(field))
    values = [getattr(element, key.name) for element in elements]
    sharing = collections.Counter(values)
    seen: collections.Counter[object] = collections.Counter()
    paths: list[str] = []
    for value in values:
        seen[value] += 1
        ordinal = f'#{seen[value]}' if sharing[value] > 1 else ''
        paths.append(f'{path}[{_key_text(value)}{ordinal}]')
    return paths


def _bare(msg: message.Message) -> message.Message:
    """A copy of `msg` as its guards' protected content reads it: every guard at its default, keyed lists by key."""
    out = type(msg)()
    out.CopyFrom(msg)
    _strip(out)
    return out


def _strip(msg: message.Message) -> None:
    for field in _descriptor(msg).fields:
        if is_guard(field):
            msg.ClearField(field.name)
        elif _holds_beneath(field):
            if _repeated(field):
                elements = getattr(msg, field.name)
                for element in elements:
                    _strip(element)
                key = key_field(_message_type(field))
                elements.sort(key=lambda element: getattr(element, key.name))
            elif msg.HasField(field.name):
                _strip(getattr(msg, field.name))


def _protected_changes(
    old: message.Message, new: message.Message, guard: descriptor.FieldDescriptor, *, keyed: bool
) -> tuple[str, ...]:
    """The fields of `guard`'s protected content that differ between `old` and `new`, both `_bare`.

    `keyed` says the element was matched by its key, which is then the same on both sides and outside the content.
    """
    marked = guard_of(guard)
    if marked is None:
        raise ValueError(f'{guard.full_name} is not a guard')
    changed = tuple(
        field.name
        for field in _descriptor(new).fields
        if not is_guard(field)
        and not (keyed and is_element_key(field))
        and field.name not in marked.ignores
        and _encoding(old, field) != _encoding(new, field)
    )
    return (*changed, UNKNOWN_FIELDS) if _unknown(old) != _unknown(new) else changed


def _guards(desc: descriptor.Descriptor) -> list[descriptor.FieldDescriptor]:
    return [field for field in desc.fields if is_guard(field)]


def _judge(old: message.Message, new: message.Message, path: str, found: list[Violation], *, keyed: bool) -> None:
    """Hold each guard of an element present in `old` and `new` to the rule."""
    desc = _descriptor(new)
    bare_old, bare_new = _bare(old), _bare(new)
    for guard in _guards(desc):
        where = _join(path, guard.name)
        before, after = _encoding(old, guard), _encoding(new, guard)
        changed = _protected_changes(bare_old, bare_new, guard, keyed=keyed)
        if after and not before:
            found.append(Violation(where, SET))
        elif after and changed:
            found.append(Violation(where, NOT_CLEARED, changed))
        elif before != after and not changed:
            found.append(Violation(where, CHANGED))


def _compare(
    old: message.Message | None, new: message.Message, path: str, found: list[Violation], *, keyed: bool = False
) -> None:
    desc = _descriptor(new)
    if _unknown(new) != (b'' if old is None else _unknown(old)):
        found.append(Violation(path or desc.name, UNKNOWN_CHANGED))
    if old is None:
        found.extend(Violation(_join(path, guard.name), SET_ON_NEW) for guard in _guards(desc) if _encoding(new, guard))
    elif _guards(desc):
        _judge(old, new, path, found, keyed=keyed)
    for field in desc.fields:
        if not _holds_beneath(field):
            continue
        where = _join(path, field.name)
        if _repeated(field):
            _compare_list(old, new, field, where, found)
        elif new.HasField(field.name):
            before = getattr(old, field.name) if old is not None and old.HasField(field.name) else None
            _compare(before, getattr(new, field.name), where, found)


def _keyed(elements: Sequence[message.Message], key: descriptor.FieldDescriptor) -> dict[object, message.Message]:
    """The elements by key, leaving out a key two of them share: neither can be told for the element a key names."""
    by_key: dict[object, message.Message] = {}
    shared: set[object] = set()
    for element in elements:
        value = getattr(element, key.name)
        if value in by_key:
            shared.add(value)
        by_key[value] = element
    return {value: element for value, element in by_key.items() if value not in shared}


def _compare_list(
    old: message.Message | None,
    new: message.Message,
    field: descriptor.FieldDescriptor,
    path: str,
    found: list[Violation],
) -> None:
    key = key_field(_message_type(field))
    before = {} if old is None else _keyed(getattr(old, field.name), key)
    seen: set[object] = set()
    elements = getattr(new, field.name)
    for element, where in zip(elements, _element_paths(elements, field, path), strict=True):
        value = getattr(element, key.name)
        if value in seen:
            found.append(Violation(where, DUPLICATE_KEY))
            continue
        seen.add(value)
        _compare(before.get(value), element, where, found, keyed=True)


def _encoding(msg: message.Message, field: descriptor.FieldDescriptor) -> bytes:
    """`field` of `msg` as the wire carries it: empty when unset, equal exactly when the values are."""
    single = type(msg)()
    _copy_field(msg, single, field)
    return single.SerializeToString(deterministic=True)


def _copy_field(source: message.Message, target: message.Message, field: descriptor.FieldDescriptor) -> None:
    """Set `field` of `target` to its value in `source`, leaving it unset where `source` has it unset."""
    if _repeated(field):
        values = getattr(target, field.name)
        if _is_map(field):
            values.MergeFrom(getattr(source, field.name))
        else:
            values.extend(getattr(source, field.name))
    elif _is_message(field):
        if source.HasField(field.name):
            getattr(target, field.name).CopyFrom(getattr(source, field.name))
    elif not field.has_presence or source.HasField(field.name):
        setattr(target, field.name, getattr(source, field.name))


def carry(old: message.Message | None, new: message.Message) -> tuple[message.Message, list[Cleared]]:
    """`new`, with each judgement of an element it keeps from `old` carried across where what it judges is unchanged.

    An element `new` holds under a key `old` also holds (one key only) keeps the fields this build does not know from
    `old`, and each guard whose protected content is as `old` has it; a guard whose protected content changed is left
    at its default, and reported if the user had set it. Everything else is `new`'s, and an element `old` does not
    hold is `new`'s as built. Each judgement on an element `old` holds and `new` does not, or on one of two old
    elements sharing a key, is reported cleared too. The result keeps the rule against `old` unless `new` sets a
    guard on an element of its own.

    Returns:
        The payload to write, and each judgement it clears.

    Raises:
        SchemaError: If the payload's schema is one the rule cannot read.
        ValueError: If `old` and `new` are messages of different types.
    """
    if old is None:
        return new, []
    if old.DESCRIPTOR.full_name != new.DESCRIPTOR.full_name:
        raise ValueError(f'{old.DESCRIPTOR.full_name} and {new.DESCRIPTOR.full_name} are different payloads')
    check_schema(_descriptor(new))
    cleared: list[Cleared] = []
    return _carry(old, new, '', cleared, keyed=False), cleared


def _carry(
    old: message.Message, new: message.Message, path: str, cleared: list[Cleared], *, keyed: bool
) -> message.Message:
    out = type(new)()
    out.CopyFrom(old)
    desc = _descriptor(new)
    for field in desc.fields:
        out.ClearField(field.name)
    for field in desc.fields:
        if is_guard(field):
            continue
        where = _join(path, field.name)
        if _holds_beneath(field) and _repeated(field):
            _carry_list(old, new, out, field, where, cleared)
        elif _holds_beneath(field) and old.HasField(field.name):
            if new.HasField(field.name):
                getattr(out, field.name).CopyFrom(
                    _carry(getattr(old, field.name), getattr(new, field.name), where, cleared, keyed=False)
                )
            else:
                cleared.extend(cleared_all(getattr(old, field.name), REMOVED, where))
        else:
            _copy_field(new, out, field)
    bare_old, bare_out = _bare(old), _bare(out)
    for guard in _guards(desc):
        changed = _protected_changes(bare_old, bare_out, guard, keyed=keyed)
        if not changed:
            _copy_field(old, out, guard)
        elif _encoding(old, guard):
            cleared.append(Cleared(_join(path, guard.name), changed))
    return out


def _carry_list(
    old: message.Message,
    new: message.Message,
    out: message.Message,
    field: descriptor.FieldDescriptor,
    path: str,
    cleared: list[Cleared],
) -> None:
    """Carry each element of `field` `new` holds under a key `old` holds once; report each old one it drops."""
    key = key_field(_message_type(field))
    olds = getattr(old, field.name)
    before = _keyed(olds, key)
    kept_keys = {getattr(element, key.name) for element in getattr(new, field.name)}
    for element, where in zip(olds, _element_paths(olds, field, path), strict=True):
        value = getattr(element, key.name)
        if value not in before:
            cleared.extend(cleared_all(element, SHARED_KEY, where))
        elif value not in kept_keys:
            cleared.extend(cleared_all(element, REMOVED, where))
    target = getattr(out, field.name)
    news = getattr(new, field.name)
    for element, where in zip(news, _element_paths(news, field, path), strict=True):
        kept = before.get(getattr(element, key.name))
        target.add().CopyFrom(element if kept is None else _carry(kept, element, where, cleared, keyed=True))


def _listed(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else f'{", ".join(names[:-1])} and {names[-1]}'


def explain(violation: Violation) -> str:
    """The element and field, what the change did to it, and what to do instead."""
    if violation.reason == NOT_CLEARED:
        guidance = (
            f"the change to {_listed(violation.changed)} clears the user's judgement; leave the guard at its default, "
            'and the user reviews the element again'
        )
    else:
        guidance = _GUIDANCE[violation.reason]
    return f'{violation.path} {violation.reason}; {guidance}'


_GUIDANCE = {
    SET_ON_NEW: 'leave it at its default: only the user sets it',
    SET: 'leave it at its default: only the user sets it',
    CHANGED: 'keep the value the user set; to clear a judgement, change what it is about, or remove the element',
    UNKNOWN_CHANGED: 'keep those fields as they are; removing the element, or adding it again, clears the '
    "user's judgements on it instead",
    DUPLICATE_KEY: 'give each element a key of its own',
}


def describe(asset_path: str, violation: Violation) -> str:
    """One refusal for the agent: the asset, then `explain`."""
    return f'{asset_path}: {explain(violation)}'
