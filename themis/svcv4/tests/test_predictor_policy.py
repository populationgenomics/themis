"""Tests for the frozen per-gene predictor policy: what it resolves, and what it refuses to load."""

from __future__ import annotations

import datetime
import decimal
import json
import pathlib

import pytest

from themis.rpc import vep_pb2
from themis.svcv4 import predictor_policy, predictors
from themis.svcv4.tests import responses


def _policy(tmp_path: pathlib.Path, payload: object) -> predictor_policy.Policy:
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(payload), 'utf-8')
    return predictor_policy.load_policy(path)


def _valid(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        'version': '2026-01-01',
        'default': {'predictor': 'BayesDel', 'rationale': 'r', 'source': 's'},
        'genes': [
            {'hgnc_id': 'HGNC:1', 'symbol': 'AAA', 'predictor': 'AlphaMissense', 'rationale': 'r', 'source': 's'}
        ],
    }
    return payload | overrides


def test_the_committed_policy_pins_pkd1_to_alphamissense() -> None:
    """The substantive claim of the policy: PKD1 is the gene SM6's per-gene licence is exercised for."""
    selection = predictor_policy.load_policy().for_symbol('PKD1')

    assert selection.predictor is predictors.Predictor.ALPHAMISSENSE
    assert selection.gene is not None
    assert selection.gene.hgnc_id == 'HGNC:9008'


def test_the_committed_policy_defaults_a_gene_it_names_no_entry_for() -> None:
    selection = predictor_policy.load_policy().for_symbol('NF1')

    assert selection.predictor is predictors.Predictor.BAYESDEL
    assert selection.gene is None


def test_every_resolution_carries_what_makes_it_auditable() -> None:
    """A run has to be able to say which predictor it used and on whose authority, from the result."""
    policy = predictor_policy.load_policy()

    for selection in (policy.for_symbol('PKD1'), policy.for_symbol('NF1')):
        assert selection.rationale
        assert selection.source
        assert selection.version == policy.version


def test_either_key_reaches_the_same_entry() -> None:
    """The id is the stable key and the symbol the one a caller has; they must not disagree."""
    policy = predictor_policy.load_policy()

    assert policy.for_hgnc_id('HGNC:9008') == policy.for_symbol('PKD1')


def test_a_symbol_is_matched_case_insensitively(tmp_path: pathlib.Path) -> None:
    """Falling through to the default on casing would be a silently different predictor."""
    policy = _policy(tmp_path, _valid())

    assert policy.for_symbol('aaa').predictor is predictors.Predictor.ALPHAMISSENSE


@pytest.mark.parametrize('gene', ['', '   '])
def test_an_absent_gene_is_a_missing_input_not_an_unnamed_one(gene: str) -> None:
    with pytest.raises(ValueError, match='HGNC symbol'):
        predictor_policy.load_policy().for_symbol(gene)


@pytest.mark.parametrize('hgnc_id', ['', 'PKD1', '9008', 'HGNC:'])
def test_a_lookup_by_id_holds_the_id_to_its_shape(hgnc_id: str) -> None:
    with pytest.raises(ValueError, match='HGNC id'):
        predictor_policy.load_policy().for_hgnc_id(hgnc_id)


def test_a_predictor_with_no_threshold_table_is_refused_at_load(tmp_path: pathlib.Path) -> None:
    """Accepted, it would resolve fine and fail on the variant the entry was written for."""
    unscored = next(p for p in predictors.Predictor if not predictors.implements(p))
    payload = _valid(default={'predictor': unscored.value, 'rationale': 'r', 'source': 's'})

    with pytest.raises(predictor_policy.PredictorPolicyError, match='no SVCv4 threshold table'):
        _policy(tmp_path, payload)


def test_a_predictor_outside_sm6s_seven_is_refused_at_load(tmp_path: pathlib.Path) -> None:
    payload = _valid(default={'predictor': 'PolyPhen-2', 'rationale': 'r', 'source': 's'})

    with pytest.raises(predictor_policy.PredictorPolicyError, match="not one of SM6's"):
        _policy(tmp_path, payload)


@pytest.mark.parametrize('field', ['rationale', 'source'])
def test_an_entry_that_cannot_be_audited_is_refused_at_load(tmp_path: pathlib.Path, field: str) -> None:
    """A frozen choice with no rationale or source is not frozen in any useful sense."""
    entry = {'hgnc_id': 'HGNC:1', 'symbol': 'AAA', 'predictor': 'AlphaMissense', 'rationale': 'r', 'source': 's'}
    entry[field] = ''

    with pytest.raises(predictor_policy.PredictorPolicyError, match=field):
        _policy(tmp_path, _valid(genes=[entry]))


@pytest.mark.parametrize(
    ('second', 'expected'),
    [
        ({'hgnc_id': 'HGNC:1', 'symbol': 'BBB'}, 'HGNC id'),
        ({'hgnc_id': 'HGNC:2', 'symbol': 'aaa'}, 'symbol'),
    ],
)
def test_one_gene_named_twice_is_refused_at_load(tmp_path: pathlib.Path, second: dict[str, str], expected: str) -> None:
    """A duplicate key would make a lookup the first of two answers rather than the answer."""
    genes = [
        {'hgnc_id': 'HGNC:1', 'symbol': 'AAA', 'predictor': 'AlphaMissense', 'rationale': 'r', 'source': 's'},
        {**second, 'predictor': 'BayesDel', 'rationale': 'r', 'source': 's'},
    ]

    with pytest.raises(predictor_policy.PredictorPolicyError, match=expected):
        _policy(tmp_path, _valid(genes=genes))


@pytest.mark.parametrize('hgnc_id', ['9008', 'HGNC:PKD1', 'hgnc:9008'])
def test_an_entry_keyed_by_something_that_is_not_an_hgnc_id_is_refused(tmp_path: pathlib.Path, hgnc_id: str) -> None:
    genes = [{'hgnc_id': hgnc_id, 'symbol': 'AAA', 'predictor': 'BayesDel', 'rationale': 'r', 'source': 's'}]

    with pytest.raises(predictor_policy.PredictorPolicyError, match='HGNC id'):
        _policy(tmp_path, _valid(genes=genes))


@pytest.mark.parametrize('version', ['v1', '2026-13-01', ''])
def test_an_unversioned_policy_is_refused_at_load(tmp_path: pathlib.Path, version: str) -> None:
    with pytest.raises(predictor_policy.PredictorPolicyError, match='version'):
        _policy(tmp_path, _valid(version=version))


def test_a_missing_policy_file_is_not_an_empty_policy(tmp_path: pathlib.Path) -> None:
    with pytest.raises(predictor_policy.PredictorPolicyError, match='not found'):
        predictor_policy.load_policy(tmp_path / 'absent.json')


D = decimal.Decimal
_TRANSCRIPT = 'NM_000123.4'


def _scored(
    selection: predictor_policy.Selection,
    response: vep_pb2.AnnotateResponse | None = None,
    *,
    transcript: str = _TRANSCRIPT,
) -> predictor_policy.PredictorScore:
    """The door as a caller reaches it: the request the selection built, and the answer to it."""
    return predictor_policy.mis_prd_from_vep(
        predictor_policy.annotate_request(selection, variant=f'{_TRANSCRIPT}:c.3496G>C'),
        responses.vep_response() if response is None else response,
        selection,
        transcript=transcript,
    )


def _selection(predictor: predictors.Predictor = predictors.Predictor.BAYESDEL) -> predictor_policy.Selection:
    return predictor_policy.Selection(
        predictor=predictor,
        gene=predictor_policy.Gene(hgnc_id='HGNC:1', symbol='AAA'),
        rationale='the calibration this entry rests on',
        source='the paper it is read from',
        version=datetime.date(2026, 1, 1),
    )


def test_the_request_asks_for_the_selected_predictor_and_no_other() -> None:
    request = predictor_policy.annotate_request(_selection(), variant=f'{_TRANSCRIPT}:c.3496G>C')
    assert list(request.predictors) == ['BayesDel']
    assert request.variant == f'{_TRANSCRIPT}:c.3496G>C'


def test_a_request_naming_no_variant_is_refused() -> None:
    with pytest.raises(ValueError, match='empty variant'):
        predictor_policy.annotate_request(_selection(), variant='  ')


@pytest.mark.parametrize(
    ('predictor', 'points'),
    [(predictors.Predictor.BAYESDEL, '2.0'), (predictors.Predictor.ALPHAMISSENSE, '3.0')],
)
def test_each_wire_form_is_read_at_its_own_key(predictor: predictors.Predictor, points: str) -> None:
    # One key per predictor: a first-class VEP field and a dbNSFP column reach the payload the same
    # way but under different names, and reading the wrong one scores nothing at all.
    scored = _scored(_selection(predictor))
    assert scored.points == D(points)
    assert scored.code == 'MIS_PRD'
    assert predictor.value in scored.derivation


def test_the_transcripts_own_consequence_is_the_one_read() -> None:
    # The payload annotates several transcripts, each with its own score.
    scored = _scored(_selection())
    assert scored.score == D('0.35')


def test_the_accessions_version_run_does_not_hide_the_annotation() -> None:
    scored = _scored(_selection(), transcript='NM_000123.9')
    assert scored.score == D('0.35')


def test_a_predictor_with_no_score_for_the_transcript_determines_nothing() -> None:
    # The rpc holds the predictor names to a closed set, so an absent score is the predictor having
    # none rather than a name Ensembl silently ignored.
    payload = responses.vep_payload()
    del payload['transcript_consequences'][0]['BayesDel_noAF_score']  # type: ignore[index] — the payload is JSON
    scored = _scored(_selection(), responses.vep_response(payload))
    assert scored.score is None
    assert scored.points is None
    assert 'no score' in scored.derivation


def test_a_payload_carrying_no_annotation_for_the_transcript_is_refused() -> None:
    with pytest.raises(ValueError, match=r'NM_999999\.1'):
        _scored(_selection(), transcript='NM_999999.1')


def test_a_payload_carrying_two_annotations_for_the_transcript_is_refused() -> None:
    # Two consequences on one transcript is two answers; picking one is the shopping the policy exists
    # to prevent.
    payload = responses.vep_payload()
    payload['transcript_consequences'].append(payload['transcript_consequences'][0])  # type: ignore[union-attr, index]
    with pytest.raises(ValueError, match='must carry one'):
        _scored(_selection(), responses.vep_response(payload))


def test_a_score_outside_the_predictors_published_range_is_refused() -> None:
    # A score on another predictor's scale: binning it would report a tier off the wrong table.
    payload = responses.vep_payload()
    payload['transcript_consequences'][0]['BayesDel_noAF_score'] = 3.5  # type: ignore[index] — the payload is JSON
    with pytest.raises(ValueError, match='must be in'):
        _scored(_selection(), responses.vep_response(payload))


def test_every_predictor_the_policy_may_name_has_a_key_to_read_it_at() -> None:
    """A policy entry that resolves and then has no key to read is a gene that fails on its variant."""
    for predictor in predictors.Predictor:
        if predictors.implements(predictor):
            assert predictors.score_key(predictor)
        else:
            with pytest.raises(NotImplementedError):
                predictors.score_key(predictor)


def test_a_response_fetched_without_this_predictor_is_refused() -> None:
    # The closed predictor set is the request's guarantee: read against the selection alone, a
    # response that never asked for BayesDel would delete MIS_PRD rather than fail.
    other = predictor_policy.annotate_request(
        _selection(predictors.Predictor.ALPHAMISSENSE), variant=f'{_TRANSCRIPT}:c.3496G>C'
    )
    with pytest.raises(ValueError, match='not BayesDel'):
        predictor_policy.mis_prd_from_vep(other, responses.vep_response(), _selection(), transcript=_TRANSCRIPT)
