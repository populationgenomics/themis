"""Tests for the guest's whole-message renderer.

The messages are mostly the real ones the guest receives, so the sizes and field shapes are the ones
`show` has to survive: a source's `raw` Struct, and the long prose a curated source carries. Shapes
no themis response declares — a `map<string, string>`, a repeated proto2 extension, a
`google.protobuf.Any`, a `Struct` from a foreign descriptor pool — are built here anyway: the
contract is "any protobuf message", and each one made the renderer drop, crash on, or silently pass
over a payload.

The boundedness tests carry the module's premise rather than a number. A snippet is told to reach for
`show` on every response without checking what the response holds first, so a message that renders
unbounded is a snippet whose real output scrolls off — the results it was printing lost as surely as
if they had never been read.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from google.protobuf import any_pb2, descriptor, descriptor_pb2, descriptor_pool, message, message_factory, struct_pb2

from themis.rpc import cspec_pb2, gene_disease_pb2, gnomad_pb2, literature_pb2, store_pb2
from themis.services.sandbox_worker.guest import display


def _gnomad_with_raw(keys: int, value_size: int) -> gnomad_pb2.DescribeVariantResponse:
    resp = gnomad_pb2.DescribeVariantResponse()
    resp.provenance.add(source='gnomAD GraphQL', dataset_versions=['gnomad_r4'])
    for index in range(keys):
        resp.raw[f'population_{index}'] = 'x' * value_size
    return resp


def _string_map_message() -> message.Message:
    """An empty message carrying one `map<string, string>` field named `labels`."""
    file_proto = descriptor_pb2.FileDescriptorProto(name='guest_display_test.proto', package='guest_display_test')
    file_proto.syntax = 'proto3'
    holder = file_proto.message_type.add(name='Holder')
    entry = holder.nested_type.add(name='LabelsEntry')
    entry.options.map_entry = True
    for name, number in (('key', 1), ('value', 2)):
        entry.field.add(
            name=name,
            number=number,
            type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
            label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        )
    holder.field.add(
        name='labels',
        number=1,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type_name='.guest_display_test.Holder.LabelsEntry',
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName('guest_display_test.Holder'))()


def _repeated_extension_message() -> tuple[message.Message, descriptor.FieldDescriptor]:
    """An empty proto2 message and the handle to a `repeated string` extension of it."""
    file_proto = descriptor_pb2.FileDescriptorProto(name='guest_display_ext.proto', package='guest_display_ext')
    file_proto.syntax = 'proto2'
    file_proto.message_type.add(name='Holder').extension_range.add(start=100, end=200)
    file_proto.extension.add(
        name='tags',
        number=100,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        extendee='.guest_display_ext.Holder',
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    holder = message_factory.GetMessageClass(pool.FindMessageTypeByName('guest_display_ext.Holder'))()
    return holder, pool.FindExtensionByName('guest_display_ext.tags')


def _any_holder() -> message.Message:
    """An empty message with a `string` field and a `google.protobuf.Any` field, so the two render side by side."""
    file_proto = descriptor_pb2.FileDescriptorProto(name='guest_display_any.proto', package='guest_display_any')
    file_proto.syntax = 'proto3'
    file_proto.dependency.append('google/protobuf/any.proto')
    holder = file_proto.message_type.add(name='Holder')
    holder.field.add(name='note', number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING, label=1)
    holder.field.add(
        name='payload',
        number=2,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        label=1,
        type_name='.google.protobuf.Any',
    )
    pool = descriptor_pool.DescriptorPool()
    any_proto = descriptor_pb2.FileDescriptorProto()
    any_pb2.DESCRIPTOR.CopyToProto(any_proto)
    pool.Add(any_proto)
    pool.Add(file_proto)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName('guest_display_any.Holder'))()


def test_every_set_field_is_rendered() -> None:
    reply = literature_pb2.PaperInfo(
        doc_id='d0',
        title='A paper',
        default_representation=literature_pb2.REPRESENTATION_MARKDOWN,
        has_markdown=True,
    )
    rendered = display.render(reply)
    assert 'doc_id: "d0"' in rendered
    assert 'title: "A paper"' in rendered
    assert 'default_representation: REPRESENTATION_MARKDOWN' in rendered
    assert 'has_markdown: true' in rendered


def test_a_message_holding_only_defaults_says_so() -> None:
    # Under proto3 implicit presence a `has_markdown=false` sets nothing, so text format renders it
    # as the empty string — which reads as the service having answered nothing, the failure being fixed.
    rendered = display.render(literature_pb2.PaperInfo(has_markdown=False))
    assert 'PaperInfo' in rendered
    assert 'nothing set' in rendered


def test_the_message_type_is_named() -> None:
    assert display.render(literature_pb2.PaperInfo(has_markdown=True)).startswith('themis.rpc.literature.')


def _with_unknown_field(payload: bytes) -> literature_pb2.PaperInfo:
    """A `PaperInfo` carrying `payload` in field 1007, which this build's descriptor does not name."""
    ahead = literature_pb2.PaperInfo()
    tag = b'\xfa\x3e'  # field 1007, wire type 2 (length-delimited)
    length = len(payload)
    varint = bytearray()
    while True:
        varint.append((length & 0x7F) | (0x80 if length > 0x7F else 0))
        length >>= 7
        if not length:
            break
    ahead.MergeFromString(
        literature_pb2.PaperInfo(has_markdown=True).SerializeToString() + tag + bytes(varint) + payload
    )
    return ahead


def test_a_field_this_build_does_not_know_is_still_rendered() -> None:
    # A service deployed ahead of the guest image sends fields the committed descriptor lacks; those
    # are the likeliest of all to carry the answer, and dropping them is this module's whole failure.
    assert 'surprise' in display.render(_with_unknown_field(b'surprise'))


def test_a_large_struct_is_summarised_rather_than_dumped() -> None:
    resp = _gnomad_with_raw(keys=200, value_size=64)
    rendered = display.render(resp)
    assert 'x' * 64 not in rendered
    assert len(rendered) < resp.raw.ByteSize()
    # The field must still read as set, and its keys are what makes the payload navigable.
    assert 'raw {' in rendered
    assert 'population_0' in rendered
    assert '200 key(s)' in rendered


def test_a_small_struct_is_rendered_whole() -> None:
    resp = _gnomad_with_raw(keys=2, value_size=8)
    rendered = display.render(resp)
    assert 'population_0' in rendered
    assert 'population_1' in rendered
    assert '<elided' not in rendered


def test_a_long_text_field_is_replaced_by_its_size() -> None:
    statement = gene_disease_pb2.MechanismStatement(source='GenCC', text='y' * 400_000)
    rendered = display.render(statement)
    assert 'yyyy' not in rendered
    assert '400000' in rendered
    assert 'source: "GenCC"' in rendered


def test_a_long_repeated_text_element_is_replaced_by_its_size() -> None:
    criterion = cspec_pb2.CriterionSpecification(code='PS3')
    criterion.genes.extend(['BRCA1', 'z' * 400_000])
    rendered = display.render(criterion)
    assert 'zzzz' not in rendered
    assert 'BRCA1' in rendered
    assert '400000' in rendered


def test_a_long_extension_element_is_replaced_by_its_size() -> None:
    # An extension is reachable only through the descriptor, so summarising one by field name is an
    # AttributeError — the renderer taken down by the very field it exists to keep from burying the rest.
    holder, tags = _repeated_extension_message()
    holder.Extensions[tags].extend(['BRCA1', 'z' * 400_000])  # type: ignore[index] — the descriptor IS the handle
    rendered = display.render(holder)
    assert 'zzzz' not in rendered
    assert 'BRCA1' in rendered
    assert '400000' in rendered


def test_a_long_map_value_is_replaced_by_its_size() -> None:
    holder = _string_map_message()
    holder.labels['small'] = 'kept'  # type: ignore[index] — a dynamically-built map field
    holder.labels['large'] = 'q' * 400_000  # type: ignore[index]
    rendered = display.render(holder)
    assert 'qqqq' not in rendered
    assert 'kept' in rendered
    assert '400000' in rendered


def test_an_elided_field_is_left_on_the_message() -> None:
    statement = gene_disease_pb2.MechanismStatement(source='GenCC', text='y' * 400_000)
    display.render(statement)
    assert statement.text == 'y' * 400_000


def test_a_nested_message_is_summarised_too() -> None:
    spec = cspec_pb2.VcepSpecification()
    spec.citation.version = '1.0'
    spec.citation.release_notes = 'c' * 400_000
    rendered = display.render(spec)
    assert 'cccc' not in rendered
    assert 'version: "1.0"' in rendered


def test_the_size_at_which_a_field_is_summarised_is_the_callers() -> None:
    statement = gene_disease_pb2.MechanismStatement(text='y' * 100)
    assert 'y' * 100 in display.render(statement, max_field_size=200)
    assert 'y' * 100 not in display.render(statement, max_field_size=50)


@pytest.mark.parametrize('max_field_size', [0, -1])
def test_a_non_positive_size_is_rejected(max_field_size: int) -> None:
    with pytest.raises(ValueError, match='must be positive'):
        display.render(literature_pb2.PaperInfo(), max_field_size=max_field_size)


def test_a_max_chars_too_small_to_hold_its_own_markers_is_rejected() -> None:
    with pytest.raises(ValueError, match='at least'):
        display.render(literature_pb2.PaperInfo(), max_chars=10)


def _huge_unknown_field() -> message.Message:
    return _with_unknown_field(b'u' * 500_000)


def _many_short_elements() -> message.Message:
    # Each element is far below max_field_size, so per-field elision leaves every one of them whole.
    criterion = cspec_pb2.CriterionSpecification(code='PS3')
    criterion.genes.extend(f'GENE{index}' for index in range(5_000))
    return criterion


def _oversized_map_key() -> message.Message:
    # A Struct's keys are map keys, and a key floods the rendering exactly as a value would.
    resp = gnomad_pb2.DescribeVariantResponse()
    resp.provenance.add(source='gnomAD GraphQL', dataset_versions=['gnomad_r4'])
    resp.raw['k' * 400_000] = 'v'
    return resp


@pytest.mark.parametrize(
    ('build', 'bulk'),
    [
        (_huge_unknown_field, 'u' * 1_000),
        (_many_short_elements, 'GENE4999'),
        (_oversized_map_key, 'k' * 1_000),
    ],
    ids=['unknown-field', 'many-elements', 'map-key'],
)
def test_the_rendering_is_bounded_however_the_message_grows(build: Callable[[], message.Message], bulk: str) -> None:
    """The bound is the module's whole premise: a snippet reaches for `show` without looking first.

    Per-field elision does not deliver it. A field walk cannot reach an unknown field or a map key,
    and thousands of individually short elements are each under any per-field limit. `bulk` is a
    piece of what made each message oversized, so a pass cannot come from rendering it all.
    """
    rendered = display.render(build())
    assert len(rendered) <= display.DEFAULT_MAX_CHARS
    assert bulk not in rendered


def test_unknown_fields_too_large_to_render_are_named_rather_than_cut() -> None:
    # Cut off the end they would read as absent, which is the reading this module exists to end.
    rendered = display.render(_huge_unknown_field())
    assert 'unknown fields' in rendered
    assert '500005 bytes' in rendered  # their size, so they read as set and worth another look


def test_a_cheap_unknown_field_keeps_its_content_when_known_fields_overflow() -> None:
    """Unknown fields are the likeliest to carry the unexpected answer, so they are not the first thing dropped.

    A known repeated field that overruns the budget must not cost a few bytes of unknown field their
    content — that inverts the priority the module states.
    """
    ahead = _with_unknown_field(b'surprise')
    for index in range(5_000):
        ahead.files.add(name=f'file-{index}.pdf', media_type='application/pdf')
    rendered = display.render(ahead)
    assert len(rendered) <= display.DEFAULT_MAX_CHARS
    assert 'surprise' in rendered
    assert 'truncated' in rendered


def test_the_elision_of_one_field_leaves_the_others_readable() -> None:
    """The bound is not the point on its own — a rendering that is all marker has buried the answer.

    An oversized map key once did exactly that: within budget, and with every other field of the
    response gone.
    """
    rendered = display.render(_oversized_map_key())
    assert 'gnomAD GraphQL' in rendered
    assert 'gnomad_r4' in rendered
    assert 'k' * 1_000 not in rendered
    assert '400000 chars' in rendered  # the key is named and sized, not printed


def test_a_long_map_key_is_re_seated_under_a_shortened_one() -> None:
    holder = _string_map_message()
    holder.labels['q' * 400_000] = 'kept'  # type: ignore[index] — a dynamically-built map field
    holder.labels['short'] = 'also kept'  # type: ignore[index]
    rendered = display.render(holder)
    assert len(rendered) <= display.DEFAULT_MAX_CHARS
    assert 'kept' in rendered
    assert 'also kept' in rendered
    assert '400000 chars' in rendered


def test_two_long_map_keys_stay_two_entries() -> None:
    """Shortening keys must not collide them: a shared prefix would silently drop an entry."""
    holder = _string_map_message()
    for suffix in ('a', 'b'):
        holder.labels['q' * 400_000 + suffix] = f'value-{suffix}'  # type: ignore[index]
    rendered = display.render(holder)
    assert 'value-a' in rendered
    assert 'value-b' in rendered


def test_the_truncated_body_leaves_no_token_or_block_open() -> None:
    """Cut mid-token the marker reads as a field's value; cut mid-block it reads as a field *of* that block.

    An unbalanced brace also makes the half-rendered submessage look like one that was set and empty,
    which is the same misreading in a different place.
    """
    info = literature_pb2.PaperInfo()
    for index in range(5_000):
        info.files.add(name=f'file-{index}.pdf', media_type='application/pdf')
    rendered = display.render(info)
    body, _, marker = rendered.rpartition('<truncated:')
    assert marker  # the truncation path is the one under test
    assert body.endswith('\n')
    assert body.count('"') % 2 == 0  # no quote left open by the cut
    assert body.count('{') == body.count('}')


def test_a_head_survives_a_first_field_larger_than_the_whole_budget() -> None:
    """The rendering has to be non-trivial, not merely bounded — a bound is also met by keeping nothing.

    A response whose content all sits inside one top-level submessage returns to depth 0 only on its final
    line, so a cut that refused to leave any block open would keep none of it: the caller would get the type
    name and a marker reporting zero characters rendered. `ListSpecifications` is that shape today.
    """
    resp = cspec_pb2.ListSpecificationsResponse()
    spec = resp.specifications.add(title='BRCA1 VCEP')
    for index in range(200):
        spec.criteria.add(code=f'PS{index}', instructions=['x' * 300])
    rendered = display.render(resp)
    body, _, marker = rendered.rpartition('<truncated:')
    assert marker  # the truncation path is the one under test
    assert 'PS0' in body  # the head is there, not just the header
    assert len(body) > display.DEFAULT_MAX_CHARS // 2  # and it is most of the budget, not a token amount
    assert body.count('{') == body.count('}')
    assert len(rendered) <= display.DEFAULT_MAX_CHARS


def test_an_oversized_any_type_url_does_not_bury_its_siblings() -> None:
    """The `Any` marker embeds the type it held, so that string needs the same cap a map key does.

    Uncapped it made the marker the whole rendering, and a bare `Any` came back as nothing but a
    truncation marker — the field conveying less than if it had been dropped.
    """
    holder = _any_holder()
    holder.note = 'KEEP ME'  # type: ignore[attr-defined] — dynamically built
    holder.payload.type_url = 't' * 400_000  # type: ignore[attr-defined]
    holder.payload.value = b'x' * 5_000  # type: ignore[attr-defined]
    rendered = display.render(holder)
    assert 'KEEP ME' in rendered
    assert 't' * 1_000 not in rendered
    assert '400000 chars' in rendered  # the type is named and sized
    assert rendered.count('{') == rendered.count('}')


def test_a_key_crafted_to_equal_another_s_shortened_form_loses_neither() -> None:
    """No map entry is lost to shortening, whatever the keys are — the invariant, through the public path."""
    holder = _string_map_message()
    original = 'q' * 400_000
    holder.labels[original] = 'from the long key'  # type: ignore[index]
    # The second key IS the first's shortened form — the collision has to be constructed to be tested.
    holder.labels[display._short(original)] = 'from the collider'  # type: ignore[index]
    rendered = display.render(holder)
    assert 'from the long key' in rendered
    assert 'from the collider' in rendered


def test_shortening_keys_lifts_them_all_out_before_putting_any_back() -> None:
    """The hazardous order, made deterministic: a proto map iterates arbitrarily, a dict by insertion.

    Reseating one key at a time writes the long key's short form over the entry already sitting
    there, and only then deletes the long key — so the collider's value is gone. Which entry is lost
    depends on iteration order, which is why the property is pinned here rather than on a proto map.
    """
    original = 'q' * 400_000
    entries: dict[object, object] = {original: 'from the long key', display._short(original): 'from the collider'}
    display._reseat_long_keys(entries)
    assert sorted(str(value) for value in entries.values()) == ['from the collider', 'from the long key']
    assert all(len(str(key)) > display._MAX_SHORT_CHARS for key in entries)


def test_an_unknown_field_nested_in_a_submessage_is_named_by_size() -> None:
    """The lift-off-the-end trick only works at the top level; nested, the content is given up, never garbled."""
    info = literature_pb2.PaperInfo(has_markdown=True)
    nested = info.files.add(name='f.pdf')
    nested.MergeFromString(nested.SerializeToString() + b'\xfa\x3e\x08surprise')
    for index in range(5_000):
        info.files.add(name=f'file-{index}.pdf', media_type='application/pdf')
    rendered = display.render(info)
    assert len(rendered) <= display.DEFAULT_MAX_CHARS
    assert 'unknown fields' in rendered
    assert rendered.count('{') == rendered.count('}')


def test_a_budget_the_message_name_alone_exhausts_is_rejected() -> None:
    """`max_chars` is the whole rendering, header included, so a long full name can leave no body at all."""
    name = 'n' * 60
    file_proto = descriptor_pb2.FileDescriptorProto(name='guest_display_long.proto', package='.'.join([name] * 6))
    file_proto.syntax = 'proto3'
    file_proto.message_type.add(name='Holder')
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    holder = message_factory.GetMessageClass(pool.FindMessageTypeByName(f'{".".join([name] * 6)}.Holder'))()
    assert len(holder.DESCRIPTOR.full_name) > 365
    with pytest.raises(ValueError, match='leaves'):
        display.render(holder, max_chars=400)
    assert len(display.render(holder)) <= display.DEFAULT_MAX_CHARS


def test_a_long_bytes_field_is_replaced_by_a_bytes_marker() -> None:
    chunk = store_pb2.WorkspaceChunk(content=b'\x00' * 400_000)
    rendered = display.render(chunk)
    assert '400000 bytes' in rendered
    assert len(rendered) <= display.DEFAULT_MAX_CHARS


def test_a_large_list_inside_a_struct_is_summarised() -> None:
    resp = gnomad_pb2.DescribeVariantResponse()
    resp.raw['populations'] = ['z' * 64] * 200
    rendered = display.render(resp)
    assert 'z' * 64 not in rendered
    assert 'raw {' in rendered


def test_a_bare_list_value_is_summarised() -> None:
    values = struct_pb2.ListValue()
    values.extend(['w' * 64] * 200)
    rendered = display.render(values, max_field_size=100)
    assert 'w' * 64 not in rendered
    assert '200 items' in rendered


def test_an_oversized_any_renders_rather_than_failing_to_decode() -> None:
    # Text format re-parses an `Any`'s payload against the type its `type_url` names, so a marker
    # written into that payload is a decode error at render time rather than a rendering.
    packed = struct_pb2.Struct()
    for index in range(200):
        packed[f'k{index}'] = 'v' * 40
    holder = any_pb2.Any()
    holder.Pack(packed)
    rendered = display.render(holder, max_field_size=100)
    assert 'vvvv' not in rendered
    # Naming the type is not enough: text format expands an `Any` whose type_url resolves, and that
    # expansion also mentions the type while showing none of the elision.
    assert 'elided' in rendered
    assert 'google.protobuf.Struct' in rendered


def test_a_struct_from_another_descriptor_pool_is_summarised_too() -> None:
    """A well-known type is recognised by full name, not by being the generated class.

    A message parsed against a pool of its own — a descriptor fetched from a reflection service, a
    dynamically built payload — is not an instance of `struct_pb2.Struct`, and an identity test would
    hand it to the field walk with its keys and its size unnamed.
    """
    pool = descriptor_pool.DescriptorPool()
    file_proto = descriptor_pb2.FileDescriptorProto()
    struct_pb2.DESCRIPTOR.CopyToProto(file_proto)
    pool.Add(file_proto)
    foreign = message_factory.GetMessageClass(pool.FindMessageTypeByName('google.protobuf.Struct'))()
    assert not isinstance(foreign, struct_pb2.Struct)
    for index in range(200):
        foreign[f'population_{index}'] = 'x' * 64  # type: ignore[index] — the well-known-type API, by full name
    rendered = display.render(foreign)
    assert 'x' * 64 not in rendered
    assert '200 key(s)' in rendered


def test_an_any_whose_payload_does_not_decode_still_renders() -> None:
    undecodable = any_pb2.Any(type_url='type.googleapis.com/google.protobuf.Struct', value=b'\xff\xff\xff\xff')
    assert 'type_url' in display.render(undecodable)


def test_show_writes_the_rendering(capsys: pytest.CaptureFixture[str]) -> None:
    reply = literature_pb2.PaperInfo(doc_id='d0', title='A paper')
    display.show(reply)
    assert capsys.readouterr().out == display.render(reply)
