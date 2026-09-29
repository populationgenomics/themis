"""The rule an agent's change to an asset keeps, held to the cases the browser's implementation passes too.

The cases (apps/web/src/widgets/ownership-cases.test-support.json) carry a test schema of their own beside the
checklist, so the rule is exercised over shapes the checklist does not have: a guard at the top, guards in a singular
message, a tree whose nodes judge their children, and one whose guard ignores them.
"""

from __future__ import annotations

import base64
import pathlib
import re

import pytest
from google.protobuf import descriptor, descriptor_pb2, descriptor_pool, json_format, message, message_factory

from themis.widgets import asset, ownership
from themis.widgets.models import checklist_pb2
from themis.widgets.tests import ownership_cases

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CASES = ownership_cases.CASES
_BECAUSE = {':removed': ownership.REMOVED, ':shared': ownership.SHARED_KEY}
_REASONS = {
    'changed': ownership.CHANGED,
    'not_cleared': ownership.NOT_CLEARED,
    'set': ownership.SET,
    'set_on_new': ownership.SET_ON_NEW,
    'unknown_changed': ownership.UNKNOWN_CHANGED,
    'duplicate_key': ownership.DUPLICATE_KEY,
}


@pytest.fixture(scope='module')
def pool() -> descriptor_pool.DescriptorPool:
    """The default pool with the cases' test schema added, as its payloads parse through it."""
    return ownership_cases.pool()


def _payload(pool: descriptor_pool.DescriptorPool, type_name: str, encoded: dict[str, object]) -> message.Message:
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(type_name))
    if 'binary' in encoded:
        parsed = cls()
        parsed.ParseFromString(base64.b64decode(str(encoded['binary'])))
        return parsed
    body = encoded['json']
    assert isinstance(body, dict)
    return json_format.ParseDict(body, cls())


@pytest.mark.parametrize('case', _CASES['cases'], ids=[case['name'] for case in _CASES['cases']])
def test_the_rule_decides_each_case(pool: descriptor_pool.DescriptorPool, case: dict[str, object]) -> None:
    type_name = str(case['type'])
    old = None if case['old'] is None else _payload(pool, type_name, case['old'])  # type: ignore[arg-type]
    new = _payload(pool, type_name, case['new'])  # type: ignore[arg-type]
    expected = []
    for entry in case['violations']:  # type: ignore[union-attr]
        path, code, *changed = str(entry).split(' ', 2)
        fields = tuple(changed[0].split(',')) if changed else ()
        expected.append(ownership.Violation(path, _REASONS[code], fields))
    assert ownership.violations(old, new) == expected


@pytest.mark.parametrize('case', _CASES['cases'], ids=[case['name'] for case in _CASES['cases']])
def test_carrying_the_user_s_judgements_across_keeps_the_rule(
    pool: descriptor_pool.DescriptorPool, case: dict[str, object]
) -> None:
    """What the guest's helper writes keeps each judgement on what it left alone, and clears the rest.

    The only violations left are the ones the agent's own new elements carry, and each judgement cleared is reported
    with the fields whose change cleared it.
    """
    type_name = str(case['type'])
    old = None if case['old'] is None else _payload(pool, type_name, case['old'])  # type: ignore[arg-type]
    new = _payload(pool, type_name, case['new'])  # type: ignore[arg-type]
    carried, cleared = ownership.carry(old, new)
    left = ownership.violations(old, carried)
    assert set(left) <= set(ownership.violations(old, new))
    assert not [v for v in left if v.reason in (ownership.CHANGED, ownership.NOT_CLEARED, ownership.SET)]
    expected = []
    for entry in case['cleared']:  # type: ignore[union-attr]
        path, changed = str(entry).split(' ', 1)
        if changed.startswith(':'):
            expected.append(ownership.Cleared(path, because=_BECAUSE[changed]))
        else:
            expected.append(ownership.Cleared(path, tuple(changed.split(','))))
    assert cleared == expected


def test_carrying_keeps_a_tick_on_an_item_left_alone_and_clears_one_on_an_item_reworded() -> None:
    item = checklist_pb2.Checklist.Item
    old = checklist_pb2.Checklist(
        items=[item(id='a', label='same', checked=True), item(id='b', label='PM2 applies', checked=True)]
    )
    new = checklist_pb2.Checklist(
        items=[item(id='c', label='added'), item(id='b', label='PM2 does not apply'), item(id='a', label='same')]
    )
    carried, cleared = ownership.carry(old, new)
    assert isinstance(carried, checklist_pb2.Checklist)
    assert [(each.id, each.label, each.checked) for each in carried.items] == [
        ('c', 'added', False),
        ('b', 'PM2 does not apply', False),
        ('a', 'same', True),
    ]
    assert [str(each) for each in cleared] == ['items[b].checked cleared: its label changed; the user reviews it again']


def _marked_types() -> list[descriptor.Descriptor]:
    pool = descriptor_pool.Default()
    protos = sorted((_REPO_ROOT / 'schema' / 'proto' / 'themis' / 'widgets' / 'models').glob('*.proto'))
    files = [pool.FindFileByName(proto.relative_to(_REPO_ROOT / 'schema' / 'proto').as_posix()) for proto in protos]
    return [desc for file in files for desc in file.message_types_by_name.values() if asset.is_payload_type(desc)]


def test_every_payload_that_lists_user_fields_keys_the_list() -> None:
    """A list of messages holding guards keys its elements by exactly one field, and no map holds one.

    Without a key no rule can match the elements of two versions of an asset.
    """
    marked = _marked_types()
    assert marked, 'no marked payload types'
    for desc in marked:
        ownership.check_schema(desc)


@pytest.mark.parametrize('case', _CASES['schemas'], ids=[case['name'] for case in _CASES['schemas']])
def test_the_schema_check_reads_each_schema_as_the_browser_s_does(case: dict[str, object]) -> None:
    body = case['file']
    assert isinstance(body, dict)
    file = json_format.ParseDict(body, descriptor_pb2.FileDescriptorProto())
    default = descriptor_pool.Default()
    try:
        default.FindFileByName(file.name)
    except KeyError:
        default.Add(file)
    holder = default.FindMessageTypeByName(str(case['type']))
    if case['error'] is None:
        ownership.check_schema(holder)
    else:
        with pytest.raises(ownership.SchemaError, match=re.escape(str(case['error']))):
            ownership.check_schema(holder)


@pytest.mark.parametrize(('changed', 'because'), [((), ''), (('label',), ownership.REMOVED)], ids=['neither', 'both'])
def test_a_cleared_judgement_says_either_what_changed_or_why(changed: tuple[str, ...], because: str) -> None:
    with pytest.raises(ValueError, match='either which fields changed or why'):
        ownership.Cleared('items[a].checked', changed, because)
