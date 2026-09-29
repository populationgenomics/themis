"""The generator of the widget case files both languages' tests read.

Writes apps/web/src/widgets/ownership-cases.test-support.json (the ownership rule's cases, the test schema they use, and
the schemas both schema checks are held to) and apps/web/src/widgets/asset-cases.test-support.json (the wrapper cases
both readers of a file's bytes are held to). `test_case_files.py` fails when a committed file differs from what this
writes, so a case is changed here and the files regenerated:

    uv run python -m themis.widgets.tests.case_files
"""

from __future__ import annotations

import base64
import json
import pathlib
import subprocess
from collections.abc import Callable, Sequence

from google.protobuf import any_pb2, timestamp_pb2

from themis.widgets import asset
from themis.widgets.models import checklist_pb2

_WEB = pathlib.Path(__file__).resolve().parents[3] / 'apps' / 'web'
OWNERSHIP_CASES = _WEB / 'src' / 'widgets' / 'ownership-cases.test-support.json'
ASSET_CASES = _WEB / 'src' / 'widgets' / 'asset-cases.test-support.json'

_WIDGET_PROTO = 'themis/widgets/models/widget.proto'
_GUARD = '[themis.widgets.models.guard]'
_KEY = '[themis.widgets.models.element_key]'
_WIDGET = '[themis.widgets.models.widget]'
_CASES = 'themis.widgets.cases'
_CHECKLIST = 'themis.widgets.models.Checklist'
_TREE = f'{_CASES}.Tree'
_DOC_ID = '0f8fad5b-d9cb-469f-a165-70867728950e'

type Json = dict[str, object]
# What writes one case file's content.
type Generator = Callable[[], Json]

_Checklist = checklist_pb2.Checklist
_ITEM_FIELDS = _Checklist.Item.DESCRIPTOR.fields_by_name


def _field(
    name: str,
    number: int,
    kind: str,
    *,
    label: str = 'LABEL_OPTIONAL',
    type_name: str | None = None,
    guard: Sequence[str] | None = None,
    key: bool = False,
    oneof: int | None = None,
    optional: bool = False,
) -> Json:
    """A FieldDescriptorProto in proto JSON: `guard` the names the field's guard ignores, None for no guard."""
    field: Json = {'name': name, 'number': number, 'label': label, 'type': kind}
    if type_name is not None:
        field['typeName'] = type_name
    if oneof is not None:
        field['oneofIndex'] = oneof
    if optional:
        field['proto3Optional'] = True
    field['jsonName'] = ''.join(word if i == 0 else word.capitalize() for i, word in enumerate(name.split('_')))
    options: Json = {}
    if guard is not None:
        options[_GUARD] = {'ignores': list(guard)} if guard else {}
    if key:
        options[_KEY] = True
    if options:
        field['options'] = options
    return field


def _message(
    name: str,
    fields: Sequence[Json],
    *,
    widget: bool = False,
    oneofs: Sequence[str] = (),
    nested: Sequence[Json] = (),
    options: Json | None = None,
) -> Json:
    message: Json = {'name': name, 'field': list(fields)}
    if nested:
        message['nestedType'] = list(nested)
    if widget:
        message['options'] = {_WIDGET: True}
    if options is not None:
        message['options'] = options
    if oneofs:
        message['oneofDecl'] = [{'name': oneof} for oneof in oneofs]
    return message


def _file(package: str, messages: Sequence[Json], *, enums: Sequence[Json] = (), syntax: str = 'proto3') -> Json:
    file: Json = {
        'name': f'themis/widgets/cases/{package}.proto',
        'package': f'{_CASES}.{package}',
        'dependency': [_WIDGET_PROTO],
        'messageType': list(messages),
    }
    if enums:
        file['enumType'] = list(enums)
    file['syntax'] = syntax
    return file


def _schema() -> Json:
    """The test schema the rule's cases use beside the checklist, for the nesting and guards the checklist lacks."""
    stamps = _message(
        'StampsEntry',
        [_field('key', 1, 'TYPE_STRING'), _field('value', 2, 'TYPE_MESSAGE', type_name=f'.{_CASES}.Stamp')],
        options={'mapEntry': True},
    )
    repeated = 'LABEL_REPEATED'
    return {
        'name': 'themis/widgets/cases/ownership_cases.proto',
        'package': _CASES,
        'dependency': [_WIDGET_PROTO],
        'messageType': [
            _message(
                'Tree',
                [
                    _field('title', 1, 'TYPE_STRING'),
                    _field('nodes', 2, 'TYPE_MESSAGE', label=repeated, type_name=f'.{_CASES}.Node'),
                    _field('scoped', 3, 'TYPE_MESSAGE', label=repeated, type_name=f'.{_CASES}.Scoped'),
                    _field('note', 4, 'TYPE_MESSAGE', type_name=f'.{_CASES}.Note'),
                    _field('approved', 5, 'TYPE_BOOL', guard=['nodes', 'scoped', 'note', 'featured']),
                    _field('featured', 6, 'TYPE_MESSAGE', type_name=f'.{_CASES}.Node'),
                    _field('stamps', 7, 'TYPE_MESSAGE', label=repeated, type_name=f'.{_CASES}.Tree.StampsEntry'),
                ],
                widget=True,
                nested=[stamps],
            ),
            _message(
                'Node',
                [
                    _field('id', 1, 'TYPE_STRING', key=True),
                    _field('claim', 2, 'TYPE_STRING'),
                    _field('children', 3, 'TYPE_MESSAGE', label=repeated, type_name=f'.{_CASES}.Node'),
                    _field('accepted', 4, 'TYPE_BOOL', guard=[]),
                    _field('memo', 5, 'TYPE_MESSAGE', type_name=f'.{_CASES}.Note'),
                ],
            ),
            _message(
                'Scoped',
                [
                    _field('id', 1, 'TYPE_STRING', key=True),
                    _field('claim', 2, 'TYPE_STRING'),
                    _field('children', 3, 'TYPE_MESSAGE', label=repeated, type_name=f'.{_CASES}.Scoped'),
                    _field('accepted', 4, 'TYPE_BOOL', guard=['children', 'hint']),
                    _field('hint', 5, 'TYPE_STRING'),
                ],
            ),
            _message(
                'Note',
                [
                    _field('text', 1, 'TYPE_STRING'),
                    _field('ack', 2, 'TYPE_STRING', guard=[]),
                    _field('score', 3, 'TYPE_DOUBLE', guard=[]),
                    _field('verdict', 4, 'TYPE_STRING', guard=[], oneof=1, optional=True),
                    _field('marks', 5, 'TYPE_STRING', label=repeated, guard=[]),
                    _field('stamp', 6, 'TYPE_MESSAGE', type_name=f'.{_CASES}.Stamp', guard=[]),
                    _field('yes', 7, 'TYPE_BOOL', guard=[], oneof=0),
                    _field('no', 8, 'TYPE_BOOL', guard=[], oneof=0),
                ],
                oneofs=['answer', '_verdict'],
            ),
            _message('Stamp', [_field('by', 1, 'TYPE_STRING')]),
        ],
        'syntax': 'proto3',
    }


_DONE = _field('done', 9, 'TYPE_BOOL', guard=[])
_ID = _field('id', 1, 'TYPE_STRING', key=True)
_KINDS = [{'name': 'Kind', 'value': [{'name': 'KIND_A', 'number': 0}, {'name': 'KIND_B', 'number': 1}]}]


def _holder(
    package: str,
    element: Sequence[Json],
    *,
    holder_field: Json | None = None,
    nested: Sequence[Json] = (),
    enums: Sequence[Json] = (),
    element_oneofs: Sequence[str] = (),
    extra: Sequence[Json] = (),
) -> Json:
    """A file whose marked `Holder` lists `Element`s, by default under `elements`."""
    field = holder_field or _field(
        'elements', 1, 'TYPE_MESSAGE', label='LABEL_REPEATED', type_name=f'.{_CASES}.{package}.Element'
    )
    return _file(
        package,
        [
            _message('Holder', [field], widget=True, nested=nested),
            _message('Element', element, oneofs=element_oneofs),
            *extra,
        ],
        enums=enums,
    )


def _map_holder(package: str, *, guard_on_map: bool) -> Json:
    entry = _message(
        'ByNameEntry',
        [_field('key', 1, 'TYPE_STRING'), _field('value', 2, 'TYPE_MESSAGE', type_name=f'.{_CASES}.{package}.Element')],
        options={'mapEntry': True},
    )
    field = _field(
        'by_name',
        1,
        'TYPE_MESSAGE',
        label='LABEL_REPEATED',
        type_name=f'.{_CASES}.{package}.Holder.ByNameEntry',
        guard=[] if guard_on_map else None,
    )
    return _holder(package, [_ID] if guard_on_map else [_ID, _DONE], holder_field=field, nested=[entry])


def _enum_holder(package: str, syntax: str, *, beneath: bool) -> Json:
    """A holder with an enum field, on its element or on a message holding no guard."""
    kind = _field('kind', 2, 'TYPE_ENUM', type_name=f'.{_CASES}.{package}.Kind')
    if not beneath:
        file = _holder(package, [_ID, kind, _DONE], enums=_KINDS)
    else:
        elements = _field(
            'elements', 1, 'TYPE_MESSAGE', label='LABEL_REPEATED', type_name=f'.{_CASES}.{package}.Element'
        )
        extra = _field('extra', 2, 'TYPE_MESSAGE', type_name=f'.{_CASES}.{package}.Extra')
        file = _file(
            package,
            [
                _message('Holder', [elements, extra], widget=True),
                _message('Element', [_ID, _DONE]),
                _message('Extra', [kind]),
            ],
            enums=_KINDS,
        )
    file['syntax'] = syntax
    return file


def _schema_case(name: str, package: str, file: Json, error: str | None) -> Json:
    return {'name': name, 'file': file, 'type': f'{_CASES}.{package}.Holder', 'error': error}


def _schema_cases() -> list[Json]:
    """The schemas both schema checks are held to: `error` a fragment of the refusal, None for a schema they read."""
    key_rules = 'singular string, integer or bool'
    enum_key = _field('id', 1, 'TYPE_ENUM', type_name=f'.{_CASES}.enumkey.Kind', key=True)
    guard_key = {**_field('id', 1, 'TYPE_STRING', key=True), 'options': {_GUARD: {}, _KEY: True}}
    optional_key = _field('id', 1, 'TYPE_STRING', key=True, oneof=0, optional=True)
    beneath = _holder(
        'beneath',
        [_field('id', 1, 'TYPE_STRING'), _field('sub', 2, 'TYPE_MESSAGE', type_name=f'.{_CASES}.beneath.Sub')],
        extra=[_message('Sub', [_DONE])],
    )
    label = _field('label', 2, 'TYPE_STRING')
    return [
        _schema_case(
            'a list whose elements mark no key',
            'nokey',
            _holder('nokey', [_field('id', 1, 'TYPE_STRING'), _DONE]),
            'it marks 0',
        ),
        _schema_case(
            'a list whose elements mark two keys',
            'twokeys',
            _holder('twokeys', [_ID, _field('n', 2, 'TYPE_INT64', key=True), _DONE]),
            'it marks 2',
        ),
        _schema_case(
            'a float key', 'floatkey', _holder('floatkey', [_field('id', 1, 'TYPE_DOUBLE', key=True), _DONE]), key_rules
        ),
        _schema_case(
            'a bytes key', 'byteskey', _holder('byteskey', [_field('id', 1, 'TYPE_BYTES', key=True), _DONE]), key_rules
        ),
        _schema_case(
            'an enum key',
            'enumkey',
            _holder(
                'enumkey',
                [enum_key, _DONE],
                enums=[{'name': 'Kind', 'value': [{'name': 'KIND_UNSPECIFIED', 'number': 0}]}],
            ),
            key_rules,
        ),
        _schema_case('a guard key', 'guardkey', _holder('guardkey', [guard_key]), key_rules),
        _schema_case(
            'a repeated key',
            'repeatedkey',
            _holder('repeatedkey', [_field('id', 1, 'TYPE_STRING', key=True, label='LABEL_REPEATED'), _DONE]),
            key_rules,
        ),
        _schema_case(
            'a proto3 optional key',
            'optionalkey',
            _holder('optionalkey', [optional_key, _DONE], element_oneofs=['_id']),
            key_rules,
        ),
        _schema_case(
            'a key in a oneof',
            'oneofkey',
            _holder('oneofkey', [_field('id', 1, 'TYPE_STRING', key=True, oneof=0), _DONE], element_oneofs=['k']),
            key_rules,
        ),
        _schema_case(
            'a list whose elements hold a guard only beneath, and mark no key', 'beneath', beneath, 'it marks 0'
        ),
        _schema_case('a guard map', 'guardmap', _map_holder('guardmap', guard_on_map=True), 'a map may not be a guard'),
        _schema_case(
            'a map whose values hold guards',
            'mapvalues',
            _map_holder('mapvalues', guard_on_map=False),
            'values hold guards',
        ),
        _schema_case('a keyed list, valid', 'keyed', _holder('keyed', [_ID, _DONE]), None),
        _schema_case(
            'a guard ignoring a name its message lacks',
            'ignoresabsent',
            _holder('ignoresabsent', [_ID, label, _field('done', 9, 'TYPE_BOOL', guard=['lable'])]),
            'which is not a field of',
        ),
        _schema_case(
            'a guard ignoring the element key',
            'ignoreskey',
            _holder('ignoreskey', [_ID, _field('done', 9, 'TYPE_BOOL', guard=['id'])]),
            'the element key',
        ),
        _schema_case(
            'a guard ignoring a guard',
            'ignoresguard',
            _holder(
                'ignoresguard',
                [_ID, _field('seen', 8, 'TYPE_BOOL', guard=[]), _field('done', 9, 'TYPE_BOOL', guard=['seen'])],
            ),
            'a guard, which no guard judges anyway',
        ),
        _schema_case(
            'a guard ignoring a name twice',
            'ignorestwice',
            _holder('ignorestwice', [_ID, label, _field('done', 9, 'TYPE_BOOL', guard=['label', 'label'])]),
            'more than once',
        ),
        _schema_case(
            'a guard ignoring a field, valid',
            'ignoresvalid',
            _holder(
                'ignoresvalid',
                [_ID, label, _field('hint', 3, 'TYPE_STRING'), _field('done', 9, 'TYPE_BOOL', guard=['hint'])],
            ),
            None,
        ),
        _schema_case(
            'a guard sharing a oneof with a field that is not one',
            'mixedoneof',
            _holder(
                'mixedoneof',
                [
                    _ID,
                    _field('agent_pick', 2, 'TYPE_STRING', oneof=0),
                    _field('user_pick', 3, 'TYPE_STRING', oneof=0, guard=[]),
                ],
                element_oneofs=['pick'],
            ),
            'sharing the oneof',
        ),
        _schema_case(
            'guards alone in a oneof, valid',
            'guardoneof',
            _holder(
                'guardoneof',
                [
                    _ID,
                    _field('yes', 2, 'TYPE_BOOL', oneof=0, guard=[]),
                    _field('no', 3, 'TYPE_BOOL', oneof=0, guard=[]),
                ],
                element_oneofs=['answer'],
            ),
            None,
        ),
        _schema_case('a closed enum', 'closedenum', _enum_holder('closedenum', 'proto2', beneath=False), 'closed enum'),
        _schema_case(
            'a closed enum beneath a message holding no guard',
            'closedbeneath',
            _enum_holder('closedbeneath', 'proto2', beneath=True),
            'closed enum',
        ),
        _schema_case('an open enum, valid', 'openenum', _enum_holder('openenum', 'proto3', beneath=False), None),
    ]


def _rule_case(
    name: str,
    type_name: str,
    old: Json | None,
    new: Json,
    violations: Sequence[str],
    cleared: Sequence[str] = (),
) -> Json:
    """One agent's change: `old` and `new` as proto JSON or binary, what it breaks and what `carry` clears."""
    return {
        'name': name,
        'type': type_name,
        'old': old,
        'new': new,
        'violations': list(violations),
        'cleared': list(cleared),
    }


def _item(item_id: str, label: str = 'x', checked: bool = False, quote: str | None = None) -> Json:
    item: Json = {'id': item_id, 'label': label}
    if quote is not None:
        item['citation'] = {'docId': _DOC_ID, 'quote': quote}
    if checked:
        item['checked'] = True
    return item


def _checklist(*items: Json) -> Json:
    return {'json': {'items': list(items)}}


def _binary(data: bytes) -> Json:
    return {'binary': base64.b64encode(data).decode()}


def _items(*encoded: bytes) -> bytes:
    """A checklist's bytes holding each item's bytes as written."""
    return b''.join(bytes([0x0A, len(item)]) + item for item in encoded)


def _tree(**fields: object) -> Json:
    return {'json': fields}


def _node(node_id: str, claim: str = 'c', accepted: bool = False, children: list[Json] | None = None) -> Json:
    node: Json = {'id': node_id, 'claim': claim}
    if children is not None:
        node['children'] = children
    if accepted:
        node['accepted'] = True
    return node


def _scoped(
    scoped_id: str,
    claim: str = 'c',
    accepted: bool = False,
    children: list[Json] | None = None,
    hint: str | None = None,
) -> Json:
    scoped = _node(scoped_id, claim, accepted, children)
    if hint is not None:
        scoped['hint'] = hint
    return scoped


def _checklist_cases() -> list[Json]:
    cl, item, case = _checklist, _item, _rule_case
    return [
        case('a new checklist with nothing ticked', _CHECKLIST, None, cl(item('a'), item('b')), []),
        case(
            'a new checklist with an item ticked',
            _CHECKLIST,
            None,
            cl(item('a', checked=True)),
            ['items[a].checked set_on_new'],
        ),
        case(
            'a ticked item unchanged keeps its tick',
            _CHECKLIST,
            cl(item('a', checked=True)),
            cl(item('a', checked=True)),
            [],
        ),
        case(
            'a tick reset with nothing it judges changed',
            _CHECKLIST,
            cl(item('a', checked=True)),
            cl(item('a')),
            ['items[a].checked changed'],
        ),
        case(
            'an unchanged item ticked by the agent',
            _CHECKLIST,
            cl(item('a')),
            cl(item('a', checked=True)),
            ['items[a].checked set'],
        ),
        case(
            'a reworded label keeping the tick',
            _CHECKLIST,
            cl(item('a', 'PM2 applies', True)),
            cl(item('a', 'PM2 does not apply', True)),
            ['items[a].checked not_cleared label'],
            ['items[a].checked label'],
        ),
        case(
            'a reworded label clearing the tick',
            _CHECKLIST,
            cl(item('a', 'PM2 applies', True)),
            cl(item('a', 'PM2 does not apply')),
            [],
            ['items[a].checked label'],
        ),
        case(
            'a reworded label the agent ticks',
            _CHECKLIST,
            cl(item('a', 'old')),
            cl(item('a', 'new', True)),
            ['items[a].checked set'],
        ),
        case(
            'a changed citation quote keeping the tick',
            _CHECKLIST,
            cl(item('a', quote='one passage', checked=True)),
            cl(item('a', quote='another passage', checked=True)),
            ['items[a].checked not_cleared citation'],
            ['items[a].checked citation'],
        ),
        case(
            'a changed citation quote clearing the tick',
            _CHECKLIST,
            cl(item('a', quote='one passage', checked=True)),
            cl(item('a', quote='another passage')),
            [],
            ['items[a].checked citation'],
        ),
        case(
            'a label and a citation changed, the tick kept',
            _CHECKLIST,
            cl(item('a', 'old', True)),
            cl(item('a', 'new', True, quote='q')),
            ['items[a].checked not_cleared label,citation'],
            ['items[a].checked label,citation'],
        ),
        case(
            'another item reworded, the tick kept',
            _CHECKLIST,
            cl(item('a', checked=True), item('b')),
            cl(item('a', checked=True), item('b', 'reworded')),
            [],
        ),
        case(
            'a ticked item removed',
            _CHECKLIST,
            cl(item('a', checked=True), item('b')),
            cl(item('b')),
            [],
            ['items[a].checked :removed'],
        ),
        case('an item added again, unticked', _CHECKLIST, cl(item('b')), cl(item('a'), item('b')), []),
        case(
            'an item added again, ticked',
            _CHECKLIST,
            cl(item('b')),
            cl(item('a', checked=True), item('b')),
            ['items[a].checked set_on_new'],
        ),
        case(
            'a ticked item under a new key',
            _CHECKLIST,
            cl(item('a', checked=True)),
            cl(item('z', checked=True)),
            ['items[z].checked set_on_new'],
            ['items[a].checked :removed'],
        ),
        case(
            'items reordered, ticks kept',
            _CHECKLIST,
            cl(item('a', checked=True), item('b')),
            cl(item('b'), item('a', checked=True)),
            [],
        ),
        case(
            'two items under one key', _CHECKLIST, cl(item('a')), cl(item('a'), item('a')), ['items[a#2] duplicate_key']
        ),
        case(
            'a key two old items shared',
            _CHECKLIST,
            cl(item('a', checked=True), item('a', checked=True)),
            cl(item('a', checked=True)),
            ['items[a].checked set_on_new'],
            ['items[a#1].checked :shared', 'items[a#2].checked :shared'],
        ),
    ]


def _unknown_field_cases() -> list[Json]:
    """Changes to fields this build does not know, written as bytes: proto JSON cannot carry them."""
    item = _Checklist.Item
    kept = b'\x4a\x04kept'
    group = bytes([0xA3, 0x01, 0x08, 0x01, 0xA4, 0x01])
    plain = item(id='a', label='x').SerializeToString()
    relabelled = item(id='a', label='relabelled').SerializeToString()
    ticked = item(id='a', label='x', checked=True).SerializeToString()
    other = item(id='b', label='x').SerializeToString()
    cited = item(id='a', label='x', checked=True, citation=_Checklist.Citation(doc_id=_DOC_ID)).SerializeToString()
    # the citation with an unknown field 9 inside it
    citation = _Checklist.Citation(doc_id=_DOC_ID).SerializeToString() + kept
    cited_unknown = ticked + _length_delimited(_ITEM_FIELDS['citation'].number, citation)
    binary, case = _binary, _rule_case
    return [
        case(
            'unknown fields of a retained item dropped',
            _CHECKLIST,
            binary(_items(plain + kept, other)),
            binary(_items(plain, other)),
            ['items[a] unknown_changed'],
        ),
        case(
            'unknown fields of a retained item kept',
            _CHECKLIST,
            binary(_items(plain + kept, other)),
            binary(_items(relabelled + kept, other)),
            [],
        ),
        case(
            'an unknown group of a retained item kept',
            _CHECKLIST,
            binary(_items(plain + group, other)),
            binary(_items(relabelled + group, other)),
            [],
        ),
        case(
            'unknown fields on a new item',
            _CHECKLIST,
            binary(_items(other)),
            binary(_items(plain + kept, other)),
            ['items[a] unknown_changed'],
        ),
        case(
            'unknown fields of a ticked item dropped, the tick kept',
            _CHECKLIST,
            binary(_items(ticked + kept)),
            binary(_items(ticked)),
            ['items[a] unknown_changed', 'items[a].checked not_cleared fields this build does not know'],
            [],
        ),
        case(
            'unknown fields inside the citation of a ticked item dropped, the tick kept',
            _CHECKLIST,
            binary(_items(cited_unknown)),
            binary(_items(cited)),
            ['items[a].checked not_cleared citation'],
            ['items[a].checked citation'],
        ),
        case(
            'unknown fields at the top level dropped',
            _CHECKLIST,
            binary(_items(plain) + b'\x78\x2a'),
            binary(_items(plain)),
            ['Checklist unknown_changed'],
        ),
    ]


def _tree_cases() -> list[Json]:
    tree, node, scoped, case = _tree, _node, _scoped, _rule_case
    return [
        case(
            'a child changed under an accepted node, the acceptance kept',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', 'one')])]),
            tree(nodes=[node('a', accepted=True, children=[node('b', 'two')])]),
            ['nodes[a].accepted not_cleared children'],
            ['nodes[a].accepted children'],
        ),
        case(
            'a child changed under an accepted node, the acceptance cleared',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', 'one')])]),
            tree(nodes=[node('a', children=[node('b', 'two')])]),
            [],
            ['nodes[a].accepted children'],
        ),
        case(
            "a child's judgement is not its parent's content",
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', accepted=True)])]),
            tree(nodes=[node('a', accepted=True, children=[node('b')])]),
            ['nodes[a].children[b].accepted changed'],
        ),
        case(
            'children reordered under an accepted node, the acceptance kept',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b'), node('c')])]),
            tree(nodes=[node('a', accepted=True, children=[node('c'), node('b')])]),
            [],
        ),
        case(
            'a grandchild changed under two acceptances, both kept',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', accepted=True, children=[node('c', 'one')])])]),
            tree(nodes=[node('a', accepted=True, children=[node('b', accepted=True, children=[node('c', 'two')])])]),
            ['nodes[a].accepted not_cleared children', 'nodes[a].children[b].accepted not_cleared children'],
            ['nodes[a].children[b].accepted children', 'nodes[a].accepted children'],
        ),
        case(
            'a memo changed under an accepted node, both judgements kept',
            _TREE,
            tree(nodes=[{**node('a', accepted=True), 'memo': {'text': 'one', 'ack': 'ok'}}]),
            tree(nodes=[{**node('a', accepted=True), 'memo': {'text': 'two', 'ack': 'ok'}}]),
            ['nodes[a].accepted not_cleared memo', 'nodes[a].memo.ack not_cleared text'],
            ['nodes[a].memo.ack text', 'nodes[a].accepted memo'],
        ),
        case(
            "a memo's judgement changed, its node's acceptance kept",
            _TREE,
            tree(nodes=[{**node('a', accepted=True), 'memo': {'text': 'one', 'ack': 'ok'}}]),
            tree(nodes=[{**node('a', accepted=True), 'memo': {'text': 'one', 'ack': 'other'}}]),
            ['nodes[a].memo.ack changed'],
        ),
        case(
            'a singular element given another key, its acceptance kept',
            _TREE,
            tree(featured=node('ps3', 'x', True)),
            tree(featured=node('pm2', 'x', True)),
            ['featured.accepted not_cleared id'],
            ['featured.accepted id'],
        ),
        case(
            'a child re-keyed under an accepted node, the acceptance kept',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', 'x')])]),
            tree(nodes=[node('a', accepted=True, children=[node('b2', 'x')])]),
            ['nodes[a].accepted not_cleared children'],
            ['nodes[a].accepted children'],
        ),
        case(
            'a child removed under an accepted node, the acceptance kept',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', 'x')])]),
            tree(nodes=[node('a', accepted=True)]),
            ['nodes[a].accepted not_cleared children'],
            ['nodes[a].accepted children'],
        ),
        case(
            'a child added under an accepted node, the acceptance kept',
            _TREE,
            tree(nodes=[node('a', accepted=True, children=[node('b', 'x')])]),
            tree(nodes=[node('a', accepted=True, children=[node('b', 'x'), node('c', 'y')])]),
            ['nodes[a].accepted not_cleared children'],
            ['nodes[a].accepted children'],
        ),
        case(
            'an accepted child removed under a node',
            _TREE,
            tree(nodes=[node('a', children=[node('b', 'x', True)])]),
            tree(nodes=[node('a')]),
            [],
            ['nodes[a].children[b].accepted :removed'],
        ),
        case(
            'a node removed whose accepted children share a key, each acceptance reported',
            _TREE,
            tree(nodes=[node('a', children=[node('b', 'x', True), node('b', 'y', True)])]),
            tree(),
            [],
            ['nodes[a].children[b#1].accepted :removed', 'nodes[a].children[b#2].accepted :removed'],
        ),
        case(
            "a node reworded, its child's acceptance kept",
            _TREE,
            tree(nodes=[node('a', 'p', children=[node('b', 'x', True)])]),
            tree(nodes=[node('a', 'q', children=[node('b', 'x', True)])]),
            [],
        ),
        case(
            'an accepted singular node removed',
            _TREE,
            tree(featured=node('f', 'c', True)),
            tree(),
            [],
            ['featured.accepted :removed'],
        ),
        case(
            'a child added under an accepted node, accepted by the agent',
            _TREE,
            tree(nodes=[node('a', children=[node('b')])]),
            tree(nodes=[node('a', children=[node('b'), node('c', accepted=True)])]),
            ['nodes[a].children[c].accepted set_on_new'],
        ),
        case(
            'a child changed under a node that ignores its children, the acceptance kept',
            _TREE,
            tree(scoped=[scoped('a', accepted=True, children=[scoped('b', 'one')])]),
            tree(scoped=[scoped('a', accepted=True, children=[scoped('b', 'two')])]),
            [],
        ),
        case(
            'an ignored field changed, the acceptance kept',
            _TREE,
            tree(scoped=[scoped('a', accepted=True, hint='before')]),
            tree(scoped=[scoped('a', accepted=True, hint='after')]),
            [],
        ),
        case(
            'a scoped claim changed, the acceptance kept',
            _TREE,
            tree(scoped=[scoped('a', 'one', True)]),
            tree(scoped=[scoped('a', 'two', True)]),
            ['scoped[a].accepted not_cleared claim'],
            ['scoped[a].accepted claim'],
        ),
        case(
            'a guard at the top level, its title reworded',
            _TREE,
            tree(title='a', approved=True),
            tree(title='b', approved=True),
            ['approved not_cleared title'],
            ['approved title'],
        ),
        case(
            'a guard at the top level, a field it ignores rewritten',
            _TREE,
            tree(title='a', approved=True, note={'text': 'x'}),
            tree(title='a', approved=True, note={'text': 'y'}),
            [],
        ),
        case(
            'a nested message acknowledged, new',
            _TREE,
            tree(),
            tree(note={'text': 't', 'ack': 'ok'}),
            ['note.ack set_on_new'],
        ),
        case(
            'a nested message acknowledged, its text changed',
            _TREE,
            tree(note={'text': 'a', 'ack': 'ok'}),
            tree(note={'text': 'b', 'ack': 'ok'}),
            ['note.ack not_cleared text'],
            ['note.ack text'],
        ),
        case(
            'a nested acknowledgement changed with nothing it judges',
            _TREE,
            tree(note={'text': 'a', 'ack': 'ok'}),
            tree(note={'text': 'a', 'ack': 'changed'}),
            ['note.ack changed'],
        ),
        case(
            'a nested message acknowledged, removed', _TREE, tree(note={'ack': 'ok'}), tree(), [], ['note.ack :removed']
        ),
        case(
            "one judgement is not another's content",
            _TREE,
            tree(note={'text': 'a', 'ack': 'ok', 'score': 1}),
            tree(note={'text': 'a', 'ack': 'ok', 'score': 1}),
            [],
        ),
        case('a judged NaN kept', _TREE, tree(note={'score': 'NaN'}), tree(note={'score': 'NaN'}), []),
        case(
            'a judged zero turned negative',
            _TREE,
            tree(note={'score': 0}),
            tree(note={'score': -0.0}),
            ['note.score set'],
        ),
        case(
            'a judged value changed with nothing it judges',
            _TREE,
            tree(note={'score': 1}),
            tree(note={'score': 2}),
            ['note.score changed'],
        ),
        case(
            'a judgement changed along with what it judges',
            _TREE,
            tree(note={'text': 'a', 'ack': 'ok'}),
            tree(note={'text': 'b', 'ack': 'other'}),
            ['note.ack not_cleared text'],
            ['note.ack text'],
        ),
        case(
            "a oneof of guards moved off the user's answer",
            _TREE,
            tree(note={'yes': True}),
            tree(note={'no': True}),
            ['note.yes changed', 'note.no set'],
        ),
        case(
            'a oneof of guards carried across',
            _TREE,
            tree(note={'text': 'a', 'yes': True}),
            tree(note={'text': 'a'}),
            ['note.yes changed'],
        ),
        case(
            'a judged negative zero set on a new asset',
            _TREE,
            None,
            tree(note={'score': -0.0}),
            ['note.score set_on_new'],
        ),
        case(
            'an optional guard set on a new asset', _TREE, None, tree(note={'verdict': ''}), ['note.verdict set_on_new']
        ),
        case(
            'an optional guard set to its default where it was unset',
            _TREE,
            tree(note={'text': 'a'}),
            tree(note={'text': 'a', 'verdict': ''}),
            ['note.verdict set'],
        ),
        case('an optional guard kept', _TREE, tree(note={'verdict': 'fine'}), tree(note={'verdict': 'fine'}), []),
        case(
            'a repeated guard reordered',
            _TREE,
            tree(note={'marks': ['a', 'b']}),
            tree(note={'marks': ['b', 'a']}),
            ['note.marks changed'],
        ),
        case(
            'a message guard changed',
            _TREE,
            tree(note={'stamp': {'by': 'a'}}),
            tree(note={'stamp': {'by': 'b'}}),
            ['note.stamp changed'],
        ),
        case('a message guard set on a new asset', _TREE, None, tree(note={'stamp': {}}), ['note.stamp set_on_new']),
    ]


def ownership_cases() -> Json:
    """The ownership case file's content: the test schema, the schemas both checks read, and the rule's cases."""
    return {
        'schema': _schema(),
        'schemas': _schema_cases(),
        'cases': [*_checklist_cases(), *_unknown_field_cases(), *_tree_cases()],
    }


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _length_delimited(number: int, body: bytes) -> bytes:
    return bytes([number << 3 | 2]) + _varint(len(body)) + body


def asset_cases() -> Json:
    """The asset case file's content: each file's bytes, what the ownership rule reads them as, and if they draw."""
    url = b'type.googleapis.com/themis.widgets.models.Checklist'
    ticked = _Checklist(items=[_Checklist.Item(id='a', label='confirm a', checked=True)])
    value = ticked.SerializeToString()
    canonical = asset.encode(ticked)
    invalid = any_pb2.Any()
    invalid.Pack(_Checklist(items=[_Checklist.Item(id='a', label='x'), _Checklist.Item(id='a', label='y')]))
    stamp = any_pb2.Any()
    stamp.Pack(timestamp_pb2.Timestamp(seconds=1))
    unknown_type = any_pb2.Any(type_url='type.googleapis.com/themis.widgets.models.Pedigree', value=b'\x08\x01')
    # `checked`, a bool, written length-delimited
    mistyped_tick = _Checklist.Item(id='a', label='PM2 applies').SerializeToString() + _length_delimited(
        _ITEM_FIELDS['checked'].number, b'\x0a\x01a'
    )
    unmarked = any_pb2.Any()
    unmarked.Pack(_Checklist.Item(id='a', label='x', checked=True))
    tree_url = f'type.googleapis.com/{_TREE}'
    # `stamps` (field 7) holding "a" → Stamp{by: "b"}, and holding "a" with its value written as a varint
    stamp_entry = _length_delimited(7, b'\x0a\x01a\x12\x03\x0a\x01b')
    mistyped_entry = _length_delimited(7, b'\x0a\x01a\x10\x05')
    checklist = f'type {_CHECKLIST}'
    cases: list[tuple[str, bytes, str, bool]] = [
        ('a checklist, as encode writes it', canonical, checklist, True),
        ('a checklist failing its rules', invalid.SerializeToString(), checklist, False),
        ('an Any over a message outside the widgets package', stamp.SerializeToString(), 'no asset', False),
        ('an Any over a widget type this build does not know', unknown_type.SerializeToString(), 'malformed', False),
        (
            'a checklist whose payload does not parse',
            _length_delimited(1, url) + _length_delimited(2, b'\x0a\x05ab'),
            'malformed',
            False,
        ),
        ('an empty file', b'', 'no asset', False),
        ('a markdown file', b'# Notes\n\nNothing here is an asset.\n', 'no asset', False),
        (
            'the type URL tagged as a varint',
            b'\x08' + _varint(len(url)) + url + _length_delimited(2, value),
            'no asset',
            False,
        ),
        (
            'the value tagged as a varint',
            _length_delimited(1, url) + b'\x10' + _varint(len(value)) + value,
            'no asset',
            False,
        ),
        (
            'an unknown field whose varint runs past ten bytes',
            canonical + b'\x18' + b'\x80' * 10 + b'\x00',
            'no asset',
            False,
        ),
        (
            'the type URL, invalid UTF-8 before its last slash',
            _length_delimited(1, b'type\xff/themis.widgets.models.Checklist') + _length_delimited(2, value),
            'no asset',
            False,
        ),
        ('an unknown field after the value', canonical + b'\x18\x01', checklist, False),
        ('an unknown group after the value', canonical + b'\x1b\x08\x01\x1c', checklist, False),
        ('the value before the type URL', _length_delimited(2, value) + _length_delimited(1, url), checklist, False),
        (
            'a second type URL, the last naming the checklist',
            _length_delimited(1, b'type.googleapis.com/google.protobuf.Timestamp') + canonical,
            checklist,
            False,
        ),
        (
            "the type URL's length as an overlong varint",
            bytes([0x0A, 0x80 | len(url), 0x00]) + url + _length_delimited(2, value),
            checklist,
            False,
        ),
        (
            'a checklist whose tick is written in a wire type its schema does not give it',
            _length_delimited(1, url) + _length_delimited(2, _length_delimited(1, mistyped_tick)),
            'malformed',
            False,
        ),
        (
            'an Any over a message in the widgets package the option does not mark',
            unmarked.SerializeToString(),
            'malformed',
            False,
        ),
        (
            'a tree whose map holds a stamp',
            any_pb2.Any(type_url=tree_url, value=stamp_entry).SerializeToString(),
            f'type {_TREE}',
            True,
        ),
        (
            'a tree whose map entry holds its value in a wire type its schema does not give it',
            any_pb2.Any(type_url=tree_url, value=mistyped_entry).SerializeToString(),
            'malformed',
            False,
        ),
    ]
    return {
        'cases': [
            {'name': name, 'bytes': base64.b64encode(data).decode(), 'rule': rule, 'draws': draws}
            for name, data, rule, draws in cases
        ]
    }


def main() -> None:
    """Rewrite both case files, formatted as the web app's formatter writes JSON."""
    for path, content in ((OWNERSHIP_CASES, ownership_cases()), (ASSET_CASES, asset_cases())):
        path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + '\n', 'utf-8')
    # The web app's locked biome, never one fetched on the fly: its layout is the one its lint checks.
    biome = _WEB / 'node_modules' / '.bin' / 'biome'
    if not biome.exists():
        raise FileNotFoundError(f'{biome} is missing; run `bun install` in {_WEB}')
    subprocess.run([str(biome), 'format', '--write', str(OWNERSHIP_CASES), str(ASSET_CASES)], cwd=_WEB, check=True)  # noqa: S603


if __name__ == '__main__':
    main()
