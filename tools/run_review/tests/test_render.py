from __future__ import annotations

import dataclasses
import datetime
import pathlib
import subprocess

import pytest
import yaml
from google.protobuf import any_pb2, message

from themis.curation import rows
from themis.svcv4.models import svcv4_pb2
from themis.widgets.models import checklist_pb2, svcv4_classification_pb2
from tools.run_review.tests import curation_rows
from tools.run_review.variant_classification import reference, render

Record = svcv4_classification_pb2.Svcv4Classification


def _record() -> Record:
    return Record(
        routing=Record.Routing(rationale='missense in FBN1\nheterozygous'),
        codes=[
            Record.Code(
                code='CLN_AFF',
                status=svcv4_pb2.ASSESSMENT_STATUS_SCORED,
                points='2',
                confidence=svcv4_pb2.CONFIDENCE_LEANING,
                cells=[Record.Cell(cell_id='CLN_AFF.specific', count=2)],
            ),
            Record.Code(code='LOC_PHE', status=svcv4_pb2.ASSESSMENT_STATUS_NO_DATA, status_reason='no yield found'),
        ],
        classification=svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC,
    )


def test_a_field_the_schema_does_not_name_fails_the_rendering_at_any_depth() -> None:
    record = _record()
    # Field 1000 with a varint payload: what a newer writer's field reads as to these stubs.
    unknown = bytes([0xC0, 0x3E, 0x01])
    record.codes[0].MergeFromString(unknown)
    with pytest.raises(render.RenderError, match=r'Code field number\(s\) \[1000\]'):
        render.message_fields(record)


def test_an_enum_number_the_enum_does_not_name_fails_the_rendering() -> None:
    record = _record()
    record.codes[1].status = 99  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(render.RenderError, match=r'Code\.status enum number\(s\) \[99\]'):
        render.message_fields(record)


def _git(repository: pathlib.Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def clone(tmp_path: pathlib.Path) -> pathlib.Path:
    """A repository standing in for a run's clone, its `main` under `origin/` as the clone tool leaves it."""
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 'agent@example.org')
    _git(tmp_path, 'config', 'user.name', 'agent')
    return tmp_path


def _asset(payload: message.Message) -> bytes:
    """`payload` as the worker stores a widget asset: wrapped in an Any, its validation rules unchecked."""
    wrapped = any_pb2.Any()
    wrapped.Pack(payload)
    return wrapped.SerializeToString()


def _head(clone: pathlib.Path) -> str:
    return subprocess.run(  # noqa: S603
        ['git', '-C', str(clone), 'rev-parse', 'HEAD'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(clone: pathlib.Path, path: str, payload: bytes) -> None:
    (clone / path).parent.mkdir(parents=True, exist_ok=True)
    (clone / path).write_bytes(payload)
    _git(clone, 'add', path)
    _git(clone, 'commit', '-q', '-m', 'revision')
    _git(clone, 'update-ref', render.FINAL_REVISION, 'HEAD')


def test_the_final_record_is_rendered_from_the_tip_of_main(clone: pathlib.Path) -> None:
    _commit(clone, render.RECORD_PATH, _asset(Record(classification=svcv4_pb2.CLASSIFICATION_VUS)))
    _commit(clone, render.RECORD_PATH, _asset(_record()))
    document = yaml.safe_load(render.record(clone))
    assert document['commit'] == _head(clone)
    assert document['record']['classification'] == 'CLASSIFICATION_LIKELY_PATHOGENIC'


def test_a_record_is_laid_out_under_its_field_names_with_enums_by_member_name(clone: pathlib.Path) -> None:
    _commit(clone, render.RECORD_PATH, _asset(_record()))
    laid_out = yaml.safe_load(render.record(clone))['record']
    assert laid_out['classification'] == 'CLASSIFICATION_LIKELY_PATHOGENIC'
    assert [code['status'] for code in laid_out['codes']] == ['ASSESSMENT_STATUS_SCORED', 'ASSESSMENT_STATUS_NO_DATA']
    assert laid_out['codes'][0]['cells'][0]['cell_id'] == 'CLN_AFF.specific'


def test_a_timestamp_is_laid_out_in_rfc_3339(clone: pathlib.Path) -> None:
    retrieved = datetime.datetime(2026, 10, 16, 0, 23, 1, 446885, tzinfo=datetime.UTC)
    record = Record(codes=[Record.Code(evidence=[Record.Evidence(retrieval=Record.Retrieval())])])
    record.codes[0].evidence[0].retrieval.retrieved_at.FromDatetime(retrieved)
    _commit(clone, render.RECORD_PATH, _asset(record))
    laid_out = yaml.safe_load(render.record(clone))['record']['codes'][0]['evidence'][0]['retrieval']['retrieved_at']
    assert laid_out == '2026-10-16T00:23:01.446885Z'
    assert datetime.datetime.fromisoformat(laid_out) == retrieved


def test_a_timestamp_out_of_range_fails_the_rendering(clone: pathlib.Path) -> None:
    record = Record(codes=[Record.Code(evidence=[Record.Evidence(retrieval=Record.Retrieval())])])
    record.codes[0].evidence[0].retrieval.retrieved_at.nanos = 1_000_000_000
    _commit(clone, render.RECORD_PATH, _asset(record))
    with pytest.raises(render.RenderError, match='has no canonical JSON form'):
        render.record(clone)


def test_a_multi_line_string_is_written_as_a_block_that_reads_back_as_written(clone: pathlib.Path) -> None:
    _commit(clone, render.RECORD_PATH, _asset(_record()))
    text = render.record(clone)
    assert 'rationale: |' in text
    assert yaml.safe_load(text)['record']['routing']['rationale'] == _record().routing.rationale


@pytest.mark.parametrize(
    'rationale',
    [
        'no',
        '1.0',
        'null',
        '2026-10-16',
        '- a list?',
        'key: value',
        ' padded ',
        'a\x85b',
        'a\u2028b\nc',
        'tab\t\nx  \ny',
    ],
)
def test_a_string_yaml_would_read_as_another_type_reads_back_as_the_string(clone: pathlib.Path, rationale: str) -> None:
    _commit(clone, render.RECORD_PATH, _asset(Record(routing=Record.Routing(rationale=rationale))))
    assert yaml.safe_load(render.record(clone))['record']['routing']['rationale'] == rationale


def test_a_run_without_a_commit_is_said_to_have_left_no_record(clone: pathlib.Path) -> None:
    text = render.record(clone)
    assert 'pushed no commit to main' in text
    assert yaml.safe_load(text) == {'commit': None, 'record': None}


def test_a_run_whose_final_revision_holds_no_record_produced_nothing_to_compare(clone: pathlib.Path) -> None:
    _commit(clone, 'working_document.md', b'# draft\n')
    text = render.record(clone)
    assert 'produced nothing to compare' in text
    assert yaml.safe_load(text) == {'commit': _head(clone), 'record': None}


def test_a_record_that_does_not_decode_fails(clone: pathlib.Path) -> None:
    _commit(clone, render.RECORD_PATH, b'\xff\xff\xff')
    with pytest.raises(render.RenderError, match='holds no widget asset'):
        render.record(clone)


def test_a_record_path_holding_another_widget_fails(clone: pathlib.Path) -> None:
    _commit(clone, render.RECORD_PATH, _asset(checklist_pb2.Checklist()))
    with pytest.raises(render.RenderError, match=r'holds themis\.widgets\.models\.Checklist'):
        render.record(clone)


def test_a_bare_record_without_its_any_wrapper_fails(clone: pathlib.Path) -> None:
    _commit(clone, render.RECORD_PATH, _record().SerializeToString())
    with pytest.raises(render.RenderError, match='not an Svcv4Classification asset'):
        render.record(clone)


def test_a_submission_lays_out_its_history_and_every_row_sections_first() -> None:
    [decoded] = reference.decode(
        curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows(), earlier=(18,)))
    ).complete
    document = yaml.safe_load(render.submission(decoded))
    assert document['curator'] == 'A'
    assert [datetime.datetime.fromisoformat(h['at']) for h in document['history']] == [
        curation_rows.at(18),
        curation_rows.at(20),
    ]
    assert datetime.datetime.fromisoformat(document['submitted_at']) == curation_rows.at(20)
    assert document['history'][0]['note'] == 'submitted on day 18'
    assert list(document['rows']) == [rows.ROUTING, rows.VERDICT, 'cln_aff']
    assert document['rows']['cln_aff']['confidence'] == 'CONFIDENCE_LEANING'
    assert document['rows'][rows.ROUTING] == {
        'consequence_class': 'CONSEQUENCE_MISSENSE',
        'rationale': 'a missense change',
    }


def test_a_multi_line_note_reads_back_as_written() -> None:
    note = 'first line\n\n  indented, after a blank line\n'
    submission = dataclasses.replace(
        curation_rows.submission('A', 20, curation_rows.full_rows()),
        history=(reference.Submitted(at=curation_rows.at(20), note=note),),
    )
    [decoded] = reference.decode(curation_rows.stored(submission)).complete
    assert yaml.safe_load(render.submission(decoded))['history'][0]['note'] == note


def test_a_submission_time_without_a_time_zone_is_refused() -> None:
    submission = dataclasses.replace(
        curation_rows.submission('A', 20, curation_rows.full_rows()),
        submitted_at=datetime.datetime(2026, 8, 20, 9, 0),  # noqa: DTZ001
    )
    [decoded] = reference.decode(curation_rows.stored(submission)).complete
    with pytest.raises(ValueError, match='carries no time zone'):
        render.submission(decoded)


def test_the_registration_states_the_question_both_sides_were_given_and_who_is_left_out_or_outstanding() -> None:
    no_class = {**curation_rows.full_rows(), rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_UNSPECIFIED)}
    stored = curation_rows.stored(
        curation_rows.submission('A', 20, curation_rows.full_rows()),
        curation_rows.submission('C', 22, curation_rows.full_rows()),
        curation_rows.submission('D', 23, no_class),
        outstanding=('B',),
    )
    document = yaml.safe_load(render.registration(stored, reference.decode(stored)))
    assert document['disease'] == 'Marfan syndrome'
    assert document['mondo_id'] == 'MONDO:0007947'
    # The member name the record's entity renders its own mode under, so the reader compares like with like.
    assert document['mode_of_inheritance'] == svcv4_pb2.ModeOfInheritance.Name(
        svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT
    )
    assert document['clinical_context'] == curation_rows.REGISTRATION.clinical_context
    assert document['curators'] == {
        'in_reference': ['A', 'C'],
        'left_out': [{'curator': 'D', 'reason': 'submitted without a class'}],
        'outstanding': ['B'],
    }
