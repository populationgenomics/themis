"""The asset contract: which paths an ``::embed`` may name, and which bytes draw.

The browser applies the same rules (apps/web/src/widgets/asset.ts); here they are held for the guest, where the
asset helper writes through `encode` and the working-document linter reads through `decode`. The wrapper cases in
apps/web/src/widgets/asset-cases.test-support.json hold both sides to one reading of a file's bytes: what the
ownership rule reads the file as (`parse`), and whether it draws, each type read from this build's payloads or the
ownership cases' schema. The browser's test holds it to the same `draws`.
"""

from __future__ import annotations

import base64
import importlib
import json
import pathlib
import re
import subprocess
import sys

import pytest
from google.protobuf import (
    any_pb2,
    descriptor,
    descriptor_pb2,
    descriptor_pool,
    json_format,
    message,
    message_factory,
    timestamp_pb2,
)

from themis.widgets import asset
from themis.widgets.models import checklist_pb2
from themis.widgets.tests import case_files, ownership_cases

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PROTO_ROOT = _REPO_ROOT / 'schema' / 'proto'
_PAYLOAD_PROTOS = _PROTO_ROOT / 'themis' / 'widgets' / 'models'
_DOC_ID = '0f8fad5b-d9cb-469f-a165-70867728950e'
_TYPE_URL_PREFIX = 'type.googleapis.com/themis.widgets.models.'
_WRAPPER_CASES = json.loads(case_files.ASSET_CASES.read_text('utf-8'))['cases']
_NO_ASSET = 'no asset'
_MALFORMED = 'malformed'
_TYPE = 'type '

_Checklist = checklist_pb2.Checklist


def _checklist(*items: checklist_pb2.Checklist.Item) -> checklist_pb2.Checklist:
    return _Checklist(items=items)


def _item(item_id: str = 'ps3', label: str = 'Functional studies reviewed') -> checklist_pb2.Checklist.Item:
    return _Checklist.Item(id=item_id, label=label)


def _packed(payload: checklist_pb2.Checklist | timestamp_pb2.Timestamp) -> bytes:
    wrapped = any_pb2.Any()
    wrapped.Pack(payload)
    return wrapped.SerializeToString()


@pytest.mark.parametrize('path', ['assets/checklist.binpb', 'a.binpb', 'deep/er/path-1_x.v2.binpb'])
def test_a_path_over_the_character_set_is_admitted(path: str) -> None:
    asset.check_path(path)


@pytest.mark.parametrize(
    ('path', 'reason'),
    [
        ('', 'empty'),
        ('/workspace/assets/a.binpb', 'absolute'),
        ('assets//a.binpb', 'segment'),
        ('./a.binpb', 'segment'),
        ('../a.binpb', 'segment'),
        ('assets/', 'segment'),
        ('.git/config', '.git'),
        ('a b.binpb', 'character'),
        ('a*b*.binpb', 'character'),
        ('a\\]b.binpb', 'character'),
        ('ünï.binpb', 'character'),
    ],
)
def test_a_path_outside_the_rules_is_refused_with_its_reason(path: str, reason: str) -> None:
    with pytest.raises(asset.AssetError, match=reason):
        asset.check_path(path)


def test_a_payload_round_trips_through_its_asset() -> None:
    payload = _checklist(
        _item(),
        _Checklist.Item(
            id='pm2', label='Absent from controls', citation=_Checklist.Citation(doc_id=_DOC_ID, quote='absent')
        ),
    )
    assert asset.decode(asset.encode(payload)) == payload


@pytest.mark.parametrize(
    ('payload', 'reason'),
    [
        (_checklist(), 'at least 1'),
        (_checklist(_item('a'), _item('a')), 'id of its own'),
        (_checklist(_item('-a')), 'regex'),
        (_checklist(_item(label='')), 'at least 1 characters'),
        (_checklist(_Checklist.Item(id='a', label='x', citation=_Checklist.Citation(doc_id='nope'))), 'UUID'),
        (_checklist(*(_item(f'i{n}') for n in range(101))), 'no more than 100'),
    ],
)
def test_a_payload_failing_its_rules_is_refused_both_ways(payload: checklist_pb2.Checklist, reason: str) -> None:
    with pytest.raises(asset.AssetError, match=reason):
        asset.encode(payload)
    with pytest.raises(asset.AssetError, match=reason):
        asset.decode(_packed(payload))


def test_a_message_the_option_does_not_mark_is_not_a_payload() -> None:
    with pytest.raises(asset.AssetError, match='not a widget payload type'):
        asset.encode(timestamp_pb2.Timestamp(seconds=1))
    with pytest.raises(asset.AssetError, match=r'google\.protobuf\.Timestamp is not a widget payload type'):
        asset.decode(_packed(timestamp_pb2.Timestamp(seconds=1)))


@pytest.mark.parametrize(
    ('data', 'reason'),
    [
        (b'# a markdown file\n', 'not a serialized google.protobuf.Any'),
        (b'', 'naming a type'),
        (any_pb2.Any(type_url=f'{_TYPE_URL_PREFIX}Nope').SerializeToString(), 'this build knows'),
        (
            any_pb2.Any(type_url=f'{_TYPE_URL_PREFIX}Checklist', value=b'\x0a\xff').SerializeToString(),
            r'does not parse as themis\.widgets\.models\.Checklist',
        ),
    ],
)
def test_bytes_that_are_not_an_asset_are_refused_with_the_reason(data: bytes, reason: str) -> None:
    with pytest.raises(asset.AssetError, match=reason):
        asset.decode(data)


def test_importing_the_asset_module_alone_registers_every_marked_payload() -> None:
    """A payload the asset module does not import is a type `decode` refuses as unknown.

    Run in a fresh interpreter importing nothing else: this test's own module imports the stubs, which would
    register them in this process whatever the asset module does.
    """
    protos = sorted(proto.relative_to(_PROTO_ROOT).as_posix() for proto in _PAYLOAD_PROTOS.glob('*.proto'))
    assert protos, f'no protos under {_PAYLOAD_PROTOS}'
    check = f"""
from google.protobuf import descriptor_pool
from themis.widgets import asset
pool = descriptor_pool.Default()
marked = []
for name in {protos!r}:
    file = pool.FindFileByName(name)
    marked += [m.full_name for m in file.message_types_by_name.values() if asset.is_payload_type(m)]
assert marked, 'no marked payload in the widget protos'
for name in marked:
    asset.is_payload_type(pool.FindMessageTypeByName(name))
"""
    subprocess.run([sys.executable, '-c', check], cwd=_REPO_ROOT, check=True)  # noqa: S603


def _rule_reads(data: bytes) -> str:
    """What the ownership rule reads `data` as, in the cases' vocabulary."""
    try:
        payload = asset.parse(data)
    except asset.AssetError:
        return _MALFORMED
    return _NO_ASSET if payload is None else f'{_TYPE}{payload.DESCRIPTOR.full_name}'


def _draws(data: bytes) -> bool:
    try:
        asset.decode(data)
    except asset.AssetError:
        return False
    return True


@pytest.mark.parametrize('case', _WRAPPER_CASES, ids=[case['name'] for case in _WRAPPER_CASES])
def test_the_rule_and_the_linter_read_each_wrapper_case_as_the_browser_does(case: dict[str, object]) -> None:
    ownership_cases.pool()  # a case may wrap a type of the ownership cases' schema
    data = base64.b64decode(str(case['bytes']))
    # A case that draws as a type the rule does not read it as would be an unchecked widget, whatever the parsers say.
    assert not case['draws'] or str(case['rule']).startswith(_TYPE)
    assert _rule_reads(data) == case['rule']
    assert _draws(data) is case['draws']


def _message_types(desc: descriptor.Descriptor | descriptor.FileDescriptor) -> list[descriptor.Descriptor]:
    """Every message `desc` declares, nested ones included."""
    declared = list(
        desc.message_types_by_name.values() if isinstance(desc, descriptor.FileDescriptor) else desc.nested_types
    )
    return declared + [inner for message in declared for inner in _message_types(message)]


def test_every_type_the_widget_option_marks_is_in_the_widgets_package() -> None:
    """`parse` refuses an asset naming a widget type this build does not hold only if its name is in the package.

    Any other type it does not hold it reads as no asset, which the rule lets the agent write freely; so a payload
    declared elsewhere would be unchecked by every build that predates it. The guest contract tree is skipped: it
    holds cut copies of the stubs checked here, under the same names.
    """
    stubs = sorted(
        stub
        for stub in (_REPO_ROOT / 'themis').rglob('*_pb2.py')
        if 'guest_contract' not in stub.relative_to(_REPO_ROOT).parts
    )
    modules = [importlib.import_module('.'.join(stub.relative_to(_REPO_ROOT).with_suffix('').parts)) for stub in stubs]
    marked = [
        message.full_name
        for module in modules
        for message in _message_types(module.DESCRIPTOR)
        if asset.is_payload_type(message)
    ]
    assert marked, 'no marked payload among the generated stubs'
    assert [name for name in marked if not name.startswith(asset.WIDGET_PACKAGE)] == []


# A payload whose map values, group bodies and closed enum hold fields of their own: the wire types are checked beneath
# each as upb reads them. In a pool of its own, so no other test sees its types.
_NESTED = {
    'name': 'themis/widgets/cases/nested.proto',
    'package': 'themis.widgets.cases.nested',
    'syntax': 'proto2',
    'enumType': [{'name': 'Kind', 'value': [{'name': 'KIND_A', 'number': 0}, {'name': 'KIND_B', 'number': 1}]}],
    'messageType': [
        {
            'name': 'Holder',
            'field': [
                {
                    'name': 'by_name',
                    'number': 1,
                    'label': 'LABEL_REPEATED',
                    'type': 'TYPE_MESSAGE',
                    'typeName': '.themis.widgets.cases.nested.Holder.ByNameEntry',
                },
                {
                    'name': 'part',
                    'number': 2,
                    'label': 'LABEL_OPTIONAL',
                    'type': 'TYPE_GROUP',
                    'typeName': '.themis.widgets.cases.nested.Holder.Part',
                },
                {
                    'name': 'kind',
                    'number': 4,
                    'label': 'LABEL_OPTIONAL',
                    'type': 'TYPE_ENUM',
                    'typeName': '.themis.widgets.cases.nested.Kind',
                },
                {'name': 'label', 'number': 5, 'label': 'LABEL_OPTIONAL', 'type': 'TYPE_STRING'},
            ],
            'nestedType': [
                {
                    'name': 'ByNameEntry',
                    'field': [
                        {'name': 'key', 'number': 1, 'label': 'LABEL_OPTIONAL', 'type': 'TYPE_STRING'},
                        {
                            'name': 'value',
                            'number': 2,
                            'label': 'LABEL_OPTIONAL',
                            'type': 'TYPE_MESSAGE',
                            'typeName': '.themis.widgets.cases.nested.Sub',
                        },
                    ],
                    'options': {'mapEntry': True},
                },
                {
                    'name': 'Part',
                    'field': [{'name': 'done', 'number': 3, 'label': 'LABEL_OPTIONAL', 'type': 'TYPE_BOOL'}],
                },
            ],
        },
        {'name': 'Sub', 'field': [{'name': 'done', 'number': 1, 'label': 'LABEL_OPTIONAL', 'type': 'TYPE_BOOL'}]},
    ],
}


def _nested(value: bytes) -> message.Message:
    pool = descriptor_pool.DescriptorPool()
    pool.Add(json_format.ParseDict(_NESTED, descriptor_pb2.FileDescriptorProto()))
    holder = message_factory.GetMessageClass(pool.FindMessageTypeByName('themis.widgets.cases.nested.Holder'))()
    holder.ParseFromString(value)
    return holder


def _entry(body: bytes) -> bytes:
    """One entry of the map ``by_name``, holding `body`."""
    return bytes([0x0A, len(body)]) + body


@pytest.mark.parametrize(
    ('value', 'problem'),
    [
        (_entry(b'\x0a\x01k\x12\x02\x08\x01'), None),
        (_entry(b'\x0a\x01k\x12\x02\x0a\x00'), 'Holder.by_name carries its field done in a wire type'),
        (_entry(b'\x0a\x01k\x10\x05'), 'Holder carries an entry of its map by_name'),
        (_entry(b'\x08\x01\x12\x00'), 'Holder carries an entry of its map by_name'),
        (_entry(b'\x0a\x01k\x12\x02\x08\x01\x18\x01'), 'Holder carries an entry of its map by_name'),
        (b'\x13\x18\x01\x14', None),
        (b'\x13\x1a\x00\x14', 'Holder.part carries its field done in a wire type'),
        (b'\x20\x07', None),
        (b'\x28\x07', 'Holder carries its field label in a wire type'),
    ],
    ids=[
        "a map value's field in its own wire type",
        "a map value's field in another",
        "a map entry's value in another wire type",
        "a map entry's key in another wire type",
        'a map entry holding a third field',
        "a group's field in its own wire type",
        "a group's field in another",
        'a closed enum value this build does not know',
        'a string written as a varint',
    ],
)
def test_a_known_field_is_refused_only_in_a_wire_type_other_than_its_own(value: bytes, problem: str | None) -> None:
    holder = _nested(value)
    if problem is None:
        asset._require_wire_types(holder, 'Holder')
    else:
        with pytest.raises(asset.AssetError, match=re.escape(problem)):
            asset._require_wire_types(holder, 'Holder')
