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
_TRANSCRIPT = 'NM_000123.4'  # the MANE Select RefSeq accession the workflow holds
_ENSEMBL = 'ENST00000355739.9'  # its MANE pair, the transcript VEP annotates


def _scored(
    selection: predictor_policy.Selection,
    response: vep_pb2.AnnotateResponse | None = None,
    *,
    transcript: str = _TRANSCRIPT,
) -> predictor_policy.MisPrdScore:
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


def _with(transcript_id: str, **changes: object) -> list[responses.VepTranscript]:
    """The default transcripts, with the one named `transcript_id` changed."""
    if transcript_id not in {element.transcript_id for element in responses.VEP_TRANSCRIPTS}:
        raise KeyError(f'the default transcripts hold no {transcript_id}')
    return [
        element._replace(**changes) if element.transcript_id == transcript_id else element
        for element in responses.VEP_TRANSCRIPTS
    ]


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
def test_each_predictor_is_read_off_its_own_entry(predictor: predictors.Predictor, points: str) -> None:
    # The transcript carries an entry per predictor; reading another's would bin it off the wrong table.
    scored = _scored(_selection(predictor))
    assert scored.points == D(points)
    assert scored.code == 'MIS_PRD'
    assert predictor.value in scored.derivation


@pytest.mark.parametrize(
    ('transcript', 'mane_pair'),
    [(_TRANSCRIPT, _TRANSCRIPT), ('NM_000123', _TRANSCRIPT), (_ENSEMBL, ''), ('ENST00000355739', '')],
)
def test_the_accession_the_workflow_holds_finds_its_transcript(transcript: str, mane_pair: str) -> None:
    # The MANE RefSeq accession reaches the Ensembl transcript through its pair; an Ensembl one by id.
    # The other transcripts carry other scores, so reading the wrong one shows.
    scored = _scored(_selection(), transcript=transcript)
    assert (scored.score, scored.transcript_id, scored.mane_pair) == (D('0.35'), _ENSEMBL, mane_pair)
    assert _ENSEMBL in scored.derivation
    assert mane_pair in scored.derivation


def test_a_mane_plus_clinical_accession_finds_its_transcript() -> None:
    scored = _scored(_selection(), transcript='NM_001204425.2')
    assert (scored.score, scored.transcript_id) == (D('0.11'), 'ENST00000652225.2')


@pytest.mark.parametrize(('transcript', 'held'), [('NM_000123.9', _TRANSCRIPT), ('ENST00000355739.7', _ENSEMBL)])
def test_another_version_is_another_transcript_model_and_the_refusal_names_what_vep_holds(
    transcript: str, held: str
) -> None:
    # One rule for both namespaces: a versioned accession has to agree, so MANE drift under the frozen
    # release is refused rather than read off another model; the version VEP holds is named.
    with pytest.raises(ValueError, match='another transcript model') as caught:
        _scored(_selection(), transcript=transcript)
    assert held in str(caught.value)


@pytest.mark.parametrize('transcript', ['', 'NM_000123.4:c.3496G>C', 'AAA', 'ENSP00000351015', 'NM_000123.x'])
def test_something_other_than_a_transcript_accession_is_refused_before_matching(transcript: str) -> None:
    with pytest.raises(ValueError, match='takes a transcript accession'):
        _scored(_selection(), transcript=transcript)


def test_a_predictor_with_no_score_for_the_transcript_determines_nothing() -> None:
    # The rpc holds the predictor names to a closed set, so an absent score on an annotated
    # transcript is the predictor having none rather than a name Ensembl silently ignored.
    unscored = _with(_ENSEMBL, scores=(('AlphaMissense', 0.9812),))
    scored = _scored(_selection(), responses.vep_response(unscored))
    assert scored.score is None
    assert scored.points is None
    assert f'no score on {_ENSEMBL}' in scored.derivation


def test_an_entry_without_its_score_is_refused() -> None:
    # Presence tells a missing score from a zero, and a stated predictor with none is a broken answer.
    response = responses.vep_response()
    response.transcripts[0].scores[0].ClearField('score')
    with pytest.raises(ValueError, match='without its score'):
        _scored(_selection(), response)


@pytest.mark.parametrize('transcript', ['NM_999999.1', 'ENST00000999999', 'XM_000123.1'])
def test_an_accession_vep_did_not_annotate_is_refused(transcript: str) -> None:
    # An accession no annotated transcript matches is a request for the wrong transcript, not a
    # predictor with no score.
    with pytest.raises(ValueError, match='carries nothing for'):
        _scored(_selection(), transcript=transcript)


def test_an_accession_two_transcripts_match_is_refused() -> None:
    # Two transcripts is two answers; picking one is the shopping the policy exists to prevent.
    ambiguous = _with('ENST00000534520.5', mane_plus_clinical=_TRANSCRIPT)
    with pytest.raises(ValueError, match='must name one'):
        _scored(_selection(), responses.vep_response(ambiguous))


def test_a_score_outside_the_predictors_published_range_is_refused() -> None:
    # A score on another predictor's scale: binning it would report a tier off the wrong table.
    with pytest.raises(ValueError, match='must be in'):
        _scored(_selection(), responses.vep_response(_with(_ENSEMBL, scores=(('BayesDel', 3.5),))))


def test_two_scores_for_one_predictor_on_the_transcript_are_refused() -> None:
    twice = _with(_ENSEMBL, scores=(('BayesDel', 0.35), ('BayesDel', 0.11)))
    with pytest.raises(ValueError, match='must carry one'):
        _scored(_selection(), responses.vep_response(twice))


def test_only_a_predictor_this_build_can_bin_is_requested() -> None:
    """A policy entry the build cannot bin is a gene that would fail on its variant, so it fails first."""
    for predictor in predictors.Predictor:
        if predictors.implements(predictor):
            request = predictor_policy.annotate_request(_selection(predictor), variant=f'{_TRANSCRIPT}:c.3496G>C')
            assert list(request.predictors) == [predictor.value]
        else:
            with pytest.raises(NotImplementedError):
                predictor_policy.annotate_request(_selection(predictor), variant=f'{_TRANSCRIPT}:c.3496G>C')


def test_a_response_fetched_without_this_predictor_is_refused() -> None:
    # The closed predictor set is the request's guarantee: read against the selection alone, a
    # response that never asked for BayesDel would delete MIS_PRD rather than fail.
    other = predictor_policy.annotate_request(
        _selection(predictors.Predictor.ALPHAMISSENSE), variant=f'{_TRANSCRIPT}:c.3496G>C'
    )
    with pytest.raises(ValueError, match='not BayesDel'):
        predictor_policy.mis_prd_from_vep(other, responses.vep_response(), _selection(), transcript=_TRANSCRIPT)
