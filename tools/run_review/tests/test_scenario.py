from __future__ import annotations

import dataclasses
import pathlib

import pytest

from themis.curation import rows
from themis.evidence.models import evidence_pb2
from themis.svcv4.models import svcv4_pb2
from tools.run_review import rounds
from tools.run_review.tests import curation_rows
from tools.run_review.variant_classification import reference, scenario


def test_prepare_writes_a_kickoff_or_the_reason_a_variant_cannot_run(tmp_path: pathlib.Path) -> None:
    usable = curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows()))
    undecodable = {**curation_rows.full_rows(), 'cln_aff': b'\xff\xff'}
    unusable = dataclasses.replace(
        curation_rows.stored(curation_rows.submission('A', 20, undecodable)),
        registration=dataclasses.replace(curation_rows.REGISTRATION, variant_id='v2'),
    )

    reading = reference.Reading(submitted=(usable, unusable), unusable=(('v3', 'v3: 27 curators'),), unsubmitted=0)

    prepared = scenario.prepare(tmp_path, reading, 'p')

    assert prepared.runnable == ('v1',)
    assert [variant_id for variant_id, _ in prepared.unusable] == ['v3', 'v2']
    assert scenario.run_request(tmp_path / 'v3', 'p') is None
    request = scenario.run_request(tmp_path / 'v1', 'p')
    assert request is not None
    assert request.inputs.variant_classification.hgvs_c == 'c.7003C>T'
    assert scenario.run_request(tmp_path / 'v2', 'p') is None
    assert 'does not decode' in (tmp_path / 'v2' / scenario.UNUSABLE).read_text('utf-8')


def test_a_variant_enters_with_its_complete_submissions_and_waits_while_none_is_complete(
    tmp_path: pathlib.Path,
) -> None:
    no_class = {**curation_rows.full_rows(), rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_UNSPECIFIED)}
    partly = curation_rows.stored(
        curation_rows.submission('A', 20, no_class), curation_rows.submission('B', 22, curation_rows.full_rows())
    )
    none_complete = dataclasses.replace(
        curation_rows.stored(curation_rows.submission('A', 20, no_class)),
        registration=dataclasses.replace(curation_rows.REGISTRATION, variant_id='v2'),
    )
    reading = reference.Reading(submitted=(partly, none_complete), unusable=(), unsubmitted=0)

    prepared = scenario.prepare(tmp_path, reading, 'p')

    assert prepared.runnable == ('v1',)
    assert prepared.unusable == ()
    assert prepared.incomplete == (('v2', 'curator A submitted without a class'),)
    assert not (tmp_path / 'v2').exists()


def _kickoff(round_dir: pathlib.Path, stored: reference.StoredReference) -> rounds.Json:
    scenario.prepare(round_dir, reference.Reading(submitted=(stored,), unusable=(), unsubmitted=0), 'p')
    document = rounds.read_json(round_dir / stored.registration.variant_id / scenario.KICKOFF)
    assert document is not None
    return document


def test_nothing_from_a_submission_reaches_the_run(tmp_path: pathlib.Path) -> None:
    answered = curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows()))
    marked = 'SUBMISSION-ONLY'
    differently = {
        rows.ROUTING: curation_rows.routing(evidence_pb2.CONSEQUENCE_NONSENSE),
        rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_BENIGN),
        'cln_aff': curation_rows.workflow(f'{marked} rationale'),
        'pop_frq': curation_rows.workflow(f'{marked} second workflow'),
    }
    answered_otherwise = curation_rows.stored(
        dataclasses.replace(
            curation_rows.submission('B', 23, differently, earlier=(19,)),
            history=(
                reference.Submitted(at=curation_rows.at(19), note=f'{marked} first note'),
                reference.Submitted(at=curation_rows.at(23), note=f'{marked} second note'),
            ),
            workflows_version='2',
        ),
        curation_rows.submission('C', 25, {**differently, 'cln_aff': curation_rows.workflow(f'{marked} C')}),
        outstanding=('A',),
    )

    first = _kickoff(tmp_path / 'first', answered)
    second = _kickoff(tmp_path / 'second', answered_otherwise)

    assert first['inputs'] == second['inputs']
    assert scenario.run_request(tmp_path / 'first' / 'v1', 'p') == scenario.run_request(tmp_path / 'second' / 'v1', 'p')
    assert marked not in (tmp_path / 'second' / 'v1' / scenario.KICKOFF).read_text('utf-8')


def test_the_kickoff_records_the_last_complete_submissions_date_and_that_no_service_applies_it(
    tmp_path: pathlib.Path,
) -> None:
    no_class = {**curation_rows.full_rows(), rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_UNSPECIFIED)}
    stored = curation_rows.stored(
        curation_rows.submission('A', 20, curation_rows.full_rows()), curation_rows.submission('B', 22, no_class)
    )
    document = _kickoff(tmp_path, stored)
    assert document['date_to_pin'] == curation_rows.at(20).isoformat()
    assert document['date_applied'] is False


def test_a_variant_id_that_cannot_name_a_directory_is_refused(tmp_path: pathlib.Path) -> None:
    stored = dataclasses.replace(
        curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows())),
        registration=dataclasses.replace(curation_rows.REGISTRATION, variant_id='../escape'),
    )
    with pytest.raises(rounds.RoundError, match='cannot name a directory'):
        scenario.prepare(tmp_path, reference.Reading(submitted=(stored,), unusable=(), unsubmitted=0), 'p')


def test_a_variant_directory_prepare_did_not_write_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / 'v1').mkdir()
    with pytest.raises(rounds.RoundError, match=r'holds no kickoff\.json'):
        scenario.run_request(tmp_path / 'v1', 'p')
