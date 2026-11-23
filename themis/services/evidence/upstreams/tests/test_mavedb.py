"""MaveDB adapter: allele measurements -> candidate selection -> calibration bin -> the record passed through.

The happy path runs over committed MaveDB answers, recorded on 2026-11-23 from `api.mavedb.org/api/v1`:
the measurement lists of the ClinGen alleles that two variants register — BRCA1 `NM_007294.4:c.5123C>A`,
which one deposit scores and calibrates, and HBB `NM_000518.5:c.20A>T`, which none scores — and the
score set (`/score-sets/{urn}`) and variant record (`/variants/{urn}`) behind the scored one. The values
are as recorded and the records are shortened: prose and depositors' names are dropped, as are the GA4GH
allele objects but for the representation's mappings; the score set keeps two of its eight calibrations,
one of those three of its 477 controls, and each ClinVar list two of its twelve entries. Everything else
is built from small payloads. No test hits the network.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Awaitable, Callable, Mapping

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import mavedb
from themis.testing import mavedb as fake_mavedb

_FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent / 'fixtures' / 'mavedb.json').read_bytes())
_SCORED_IDS = _FIXTURE['scored']['clingen_allele_ids']
_UNSCORED_IDS = _FIXTURE['unscored']['clingen_allele_ids']
_RECORDED = fake_mavedb.handler(
    measurements=_FIXTURE['measurements'], score_sets=_FIXTURE['score_sets'], variants=_FIXTURE['variants']
)

_Handler = Callable[[httpx2.Request], httpx2.Response]


def _run[T](handler: _Handler, call: Callable[[httpx2.AsyncClient], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _fetch(client: httpx2.AsyncClient, *allele_ids: str) -> Awaitable[mavedb.MavedbResult]:
    return mavedb.fetch_mavedb(allele_ids, http_client=client)


def _noting(handler: _Handler, seen: list[str]) -> _Handler:
    """`handler`, with the path of every request it answers appended to `seen`."""

    def noting(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return handler(request)

    return noting


def _measurement(urn: str, *, relationship: str = 'direct', score: float | None = 1.5) -> dict[str, object]:
    """One measurement as MaveDB lists it; it leaves a null score out rather than stating it."""
    listed: dict[str, object] = {
        'variantUrn': urn,
        'scoreSetUrn': urn.partition('#')[0],
        'relationship': relationship,
        'isCurrent': True,
    }
    return listed if score is None else listed | {'score': score}


def _open_bin(odds: float) -> dict[str, object]:
    """A calibration whose one bin holds every score and carries `odds`."""
    return {'functionalClassifications': [{'range': [None, None], 'oddspathsRatio': odds}]}


def _score_set(
    urn: str, *, published: str | None = '2025-01-01', calibrations: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """A score set record, calibrated with one open bin unless `calibrations` says otherwise."""
    return {
        'urn': urn,
        'publishedDate': published,
        'scoreCalibrations': [_open_bin(4.2)] if calibrations is None else calibrations,
    }


def _mavedb(measurements: Mapping[str, list[dict[str, object]]], *score_sets: dict[str, object]) -> _Handler:
    """A MaveDB listing `measurements` per allele id, holding `score_sets` and a record for each listed variant."""
    listed = [str(measurement['variantUrn']) for answer in measurements.values() for measurement in answer]
    return fake_mavedb.handler(
        measurements=measurements,
        score_sets={str(score_set['urn']): score_set for score_set in score_sets},
        variants={urn: {'urn': urn, 'annotations': {}} for urn in listed},
    )


def test_fetch_mavedb_returns_the_scored_calibration() -> None:
    """One deposit scores the recorded allele; the bin its primary calibration puts the score in answers."""
    result = _run(_RECORDED, lambda c: _fetch(c, *_SCORED_IDS))

    assert result.score == -2.0082579967947303
    assert result.oddspath_ratio == 52.4
    assert result.acmg_criterion == 'PS3'
    assert result.acmg_strength == 'STRONG'
    assert result.source == 'MaveDB'
    assert result.dataset_versions == ('urn:mavedb:00000097-0-2',)


def test_the_record_passed_through_is_the_score_row_with_its_score_set() -> None:
    """What a deposit measured is read off `raw.scoreSet`, so the score set rides with the variant's row."""
    result = _run(_RECORDED, lambda c: _fetch(c, *_SCORED_IDS))

    raw = json.loads(json.dumps(result.raw))
    assert raw['urn'] == 'urn:mavedb:00000097-0-2#1864'
    assert raw['scores']['score_rep1'] == -2.05072417058557
    assert raw['scoreSet']['title'] == 'BRCA1 SGE Normalized Scores'
    assert raw['scoreSet']['shortDescription'] == 'BRCA1 SGE normalized scores for all exons'
    assert raw['scoreSet']['experiment']['urn'] == 'urn:mavedb:00000097-0'
    assert raw['scoreSet']['primaryPublicationIdentifiers'][0]['identifier'] == '30209399'
    assert [calibration['id'] for calibration in raw['scoreSet']['scoreCalibrations']] == [2467, 694]


def test_what_surrounds_the_clinvar_entries_of_the_record_stays() -> None:
    """An allele's other annotations, its ClinGen ids and the rest of a calibration are not ClinVar's to report."""
    result = _run(_RECORDED, lambda c: _fetch(c, *_SCORED_IDS))

    raw = json.loads(json.dumps(result.raw))
    assert raw['annotations']['ga4gh:VA.t5wq6s_ed5XpfoggzVxzcQORC0zKLNNb'] == {
        'gnomad': None,
        'vep': {
            'consequence': 'missense_variant',
            'consequenceSource': 'most_severe',
            'consequenceTerms': ['missense_variant'],
            'matchedTranscript': None,
            'sourceVersion': '116',
        },
    }
    assert len(raw['annotations']) == 5
    assert raw['molecularRepresentation']['type'] == 'CategoricalVariant'
    assert [mapping['coding']['id'] for mapping in raw['molecularRepresentation']['mappings']] == [
        'clingen:CA003252',
        'clingen:CA3273046592',
        'clingen:PA112700',
    ]
    igvf = raw['scoreSet']['scoreCalibrations'][0]
    assert igvf['controlsCount'] == 477
    assert igvf['functionalClassifications'][1]['oddspathsRatio'] == 39.4604


def test_a_deposit_held_under_a_later_allele_id_is_found() -> None:
    """A nucleotide-level deposit and a protein-level one are keyed differently, so every registered id is asked.

    MaveDB lists nothing under the first recorded id here and the deposit under the second.
    """
    result = _run(_RECORDED, lambda c: _fetch(c, 'PA2829696819', 'PA112700'))
    assert result.dataset_versions == ('urn:mavedb:00000097-0-2',)
    assert "('PA112700', 'nucleotide_encoding', 'urn:mavedb:00000097-0-2#1864', '2025-05-12')" in result.query


def test_a_deposit_reached_by_two_ids_is_read_once() -> None:
    """Both recorded ids list the one measurement, as the allele itself and as what encodes it."""
    seen: list[str] = []
    _run(_noting(_RECORDED, seen), lambda c: _fetch(c, 'CA003252', 'PA112700'))
    assert seen.count(fake_mavedb.record_path('score-sets', 'urn:mavedb:00000097-0-2')) == 1
    assert seen.count(fake_mavedb.record_path('variants', 'urn:mavedb:00000097-0-2#1864')) == 1


def test_only_the_selected_measurement_has_its_variant_record_read() -> None:
    """Selection needs each candidate's score set, not its record: one record is read however many matched."""
    seen: list[str] = []
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:test-a-1#1'), _measurement('urn:mavedb:test-b-1#1')]},
        _score_set('urn:mavedb:test-a-1', calibrations=[]),
        _score_set('urn:mavedb:test-b-1', calibrations=[]),
    )
    _run(_noting(handler, seen), lambda c: _fetch(c, 'PA1'))
    assert [path for path in seen if '/variants/' in path] == [
        fake_mavedb.record_path('variants', 'urn:mavedb:test-a-1#1')
    ]


def test_a_variant_no_deposit_scores_is_a_settled_answer() -> None:
    """Every id answered, each with an empty list, is the only shape that means "no assay covers this variant"."""
    with pytest.raises(errors.UnknownVariantError, match='no scored variant'):
        _run(_RECORDED, lambda c: _fetch(c, *_UNSCORED_IDS))


def test_no_allele_id_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match='at least one ClinGen allele id'):
        _run(_RECORDED, lambda c: mavedb.fetch_mavedb([], http_client=c))


def test_a_calibrated_deposit_is_preferred_over_an_uncalibrated_one() -> None:
    """Both score the allele; only one answers what the rpc is for, though the other is ranked first."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:test-a-1#1'), _measurement('urn:mavedb:test-b-1#1')]},
        _score_set('urn:mavedb:test-a-1', calibrations=[]),
        _score_set('urn:mavedb:test-b-1', calibrations=[_open_bin(4.2)]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:test-b-1',)
    assert result.oddspath_ratio == 4.2


def test_a_measurement_from_a_superseded_score_set_version_is_not_a_candidate() -> None:
    """A calibrated candidate answers before an uncalibrated one, so a withdrawn version would speak for the deposit."""
    handler = _mavedb(
        {
            'PA1': [
                _measurement('urn:mavedb:test-a-1#1', score=1.5) | {'isCurrent': False},
                _measurement('urn:mavedb:test-a-2#1', score=2.5) | {'isCurrent': True},
            ]
        },
        _score_set('urn:mavedb:test-a-1', calibrations=[_open_bin(4.2)]),
        _score_set('urn:mavedb:test-a-2', calibrations=[]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:test-a-2',)
    assert result.score == 2.5
    assert result.oddspath_ratio is None
    assert 'urn:mavedb:test-a-1#1' not in result.query


def test_a_scored_deposit_is_preferred_over_one_carrying_no_score_at_all() -> None:
    """A measurement with neither a calibration bin nor a score would otherwise shadow one ranked below it."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:test-a-2#1', score=None), _measurement('urn:mavedb:test-a-1#1', score=1.5)]},
        _score_set('urn:mavedb:test-a-2', published='2026-01-01', calibrations=[]),
        _score_set('urn:mavedb:test-a-1', published='2025-01-01', calibrations=[]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.score == 1.5
    assert result.dataset_versions == ('urn:mavedb:test-a-1',)


def test_an_uncalibrated_deposit_still_returns_its_score() -> None:
    """Absence of a calibration is the depositor's, not an absence of evidence: the score is the fact."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:test-a-1#1', score=1.5)]}, _score_set('urn:mavedb:test-a-1', calibrations=[])
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.score == 1.5
    assert result.dataset_versions == ('urn:mavedb:test-a-1',)
    assert result.oddspath_ratio is None
    assert result.acmg_criterion == ''
    assert result.acmg_strength == ''


def test_every_candidate_considered_rides_in_the_provenance_query() -> None:
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:test-a-1#1'), _measurement('urn:mavedb:test-b-1#1')]},
        _score_set('urn:mavedb:test-a-1', calibrations=[]),
        _score_set('urn:mavedb:test-b-1', calibrations=[_open_bin(4.2)]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    considered, _, selected = result.query.partition(' -> ')
    assert 'PA1' in considered
    assert 'urn:mavedb:test-a-1#1' in considered
    assert 'urn:mavedb:test-b-1#1' in considered
    assert selected == 'urn:mavedb:test-b-1#1'


@pytest.mark.parametrize('relationship', ['direct', 'protein_consequence', 'nucleotide_encoding'])
def test_a_measurement_of_any_relationship_is_a_candidate(relationship: str) -> None:
    """MaveDB lists the allele's own measurements and those of its consequence or its encodings; all are deposits."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:test-b-1#1', relationship=relationship)]},
        _score_set('urn:mavedb:test-b-1', calibrations=[_open_bin(4.2)]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:test-b-1',)
    assert result.oddspath_ratio == 4.2


def test_a_direct_measurement_outranks_a_related_one() -> None:
    """A nucleotide measurement reached from a protein allele is of one variant encoding the residue.

    It ranks below the direct one even though it is the more recent deposit.
    """
    handler = _mavedb(
        {
            'PA1': [_measurement('urn:mavedb:x-a-9#1', relationship='nucleotide_encoding')],
            'PA2': [_measurement('urn:mavedb:x-a-1#1', relationship='direct')],
        },
        _score_set('urn:mavedb:x-a-9', published='2026-01-01'),
        _score_set('urn:mavedb:x-a-1', published='2020-01-01'),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1', 'PA2'))
    assert result.dataset_versions == ('urn:mavedb:x-a-1',)


def test_the_more_recent_deposit_outranks_the_older_one() -> None:
    """Registration order is not evidential order: the accession that sorts first can be the 2020 one."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:a-a-1#5'), _measurement('urn:mavedb:z-a-1#5')]},
        _score_set('urn:mavedb:a-a-1', published='2020-01-01'),
        _score_set('urn:mavedb:z-a-1', published='2026-01-01'),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:z-a-1',)


def test_an_unpublished_deposit_ranks_below_every_published_one() -> None:
    """MaveDB reports an unpublished score set with a null date, which orders before every real one."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:a-a-1#1'), _measurement('urn:mavedb:z-a-1#1')]},
        _score_set('urn:mavedb:a-a-1', published=None),
        _score_set('urn:mavedb:z-a-1', published='2020-01-01'),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:z-a-1',)
    assert result.query.index('urn:mavedb:z-a-1#1') < result.query.index('urn:mavedb:a-a-1#1')


def test_an_unpublished_deposit_answers_when_no_published_one_scores_the_allele() -> None:
    """A null publishedDate demotes rather than drops: an unpublished score set still answers alone."""
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:a-a-1#1')]},
        _score_set('urn:mavedb:a-a-1', published=None, calibrations=[_open_bin(4.2)]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:a-a-1',)
    assert result.oddspath_ratio == 4.2


def test_the_callers_own_allele_outranks_a_derived_one_whatever_their_dates() -> None:
    """A coding expression registers the caller's change and, derived from it, its consequence.

    Both can reach a deposit directly — of different things. The one keyed on the change the caller
    actually asked about answers, or a protein-level deposit published a day later would take over
    the coding form's answer, which is the form callers are told to send *because* it matches their
    nucleotide change.
    """
    handler = _mavedb(
        {'CA1': [_measurement('urn:mavedb:nt-a-1#1')], 'PA1': [_measurement('urn:mavedb:aa-a-1#1')]},
        _score_set('urn:mavedb:nt-a-1', published='2020-01-01'),
        _score_set('urn:mavedb:aa-a-1', published='2026-01-01'),
    )
    result = _run(handler, lambda c: _fetch(c, 'CA1', 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:nt-a-1',)


def test_a_deposit_reached_under_two_alleles_is_credited_to_the_more_direct_one() -> None:
    """MaveDB labels a measurement against whichever id reached it, so one deposit arrives twice."""
    handler = _mavedb(
        {
            'CA1': [
                _measurement('urn:mavedb:x-a-1#1', relationship='protein_consequence'),
                _measurement('urn:mavedb:y-a-1#1', relationship='protein_consequence'),
            ],
            'PA1': [_measurement('urn:mavedb:x-a-1#1', relationship='direct')],
        },
        _score_set('urn:mavedb:x-a-1', published='2020-01-01'),
        _score_set('urn:mavedb:y-a-1', published='2026-01-01'),
    )
    # Credited to PA1's direct match rather than CA1's related one, x-a-1 outranks the newer related rival.
    result = _run(handler, lambda c: _fetch(c, 'CA1', 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:x-a-1',)


@pytest.mark.parametrize(
    ('score_sets', 'variants'),
    [
        ({}, {'urn:mavedb:gone-a-1#1': {'urn': 'urn:mavedb:gone-a-1#1', 'annotations': {}}}),
        ({'urn:mavedb:gone-a-1': _score_set('urn:mavedb:gone-a-1')}, {}),
    ],
    ids=['score set', 'variant'],
)
def test_a_dangling_urn_is_the_upstreams_fault_not_the_callers(
    score_sets: dict[str, object], variants: dict[str, object]
) -> None:
    """The URN came from MaveDB's own measurement list; blaming the request would send a caller rewriting it."""
    handler = fake_mavedb.handler(
        measurements={'PA1': [_measurement('urn:mavedb:gone-a-1#1')]}, score_sets=score_sets, variants=variants
    )
    with pytest.raises(ValueError, match='which its own measurements named'):
        _run(handler, lambda c: _fetch(c, 'PA1'))


def test_the_candidate_order_does_not_follow_the_payloads_own_order() -> None:
    """`oddspath_ratio` feeds the SVCv4 arithmetic, so two deposits must not answer by array order."""
    newer = _measurement('urn:mavedb:z-a-1#1')
    older = _measurement('urn:mavedb:a-a-1#1')
    score_sets = (
        _score_set('urn:mavedb:z-a-1', published='2026-01-01', calibrations=[_open_bin(4.2)]),
        _score_set('urn:mavedb:a-a-1', published='2020-01-01', calibrations=[_open_bin(9.9)]),
    )
    forward = _run(_mavedb({'PA1': [newer, older]}, *score_sets), lambda c: _fetch(c, 'PA1'))
    reversed_order = _run(_mavedb({'PA1': [older, newer]}, *score_sets), lambda c: _fetch(c, 'PA1'))
    assert forward.oddspath_ratio == 4.2
    assert reversed_order.oddspath_ratio == 4.2


def test_same_date_deposits_break_ties_by_urn_rather_than_payload_order() -> None:
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:b-a-1#1'), _measurement('urn:mavedb:a-a-1#1')]},
        _score_set('urn:mavedb:a-a-1', published='2025-05-08', calibrations=[_open_bin(4.2)]),
        _score_set('urn:mavedb:b-a-1', published='2025-05-08', calibrations=[_open_bin(9.9)]),
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:a-a-1',)
    assert result.oddspath_ratio == 4.2


@pytest.mark.parametrize(
    ('listed', 'match'),
    [
        ({'detail': 'no such allele'}, 'not a list'),
        (['not an object'], 'non-object measurement'),
        ([{'scoreSetUrn': 'urn:mavedb:test-a-1', 'relationship': 'direct'}], 'naming no variant'),
        ([{'variantUrn': 'urn:mavedb:test-a-1#1', 'relationship': 'direct'}], 'names no score set'),
        ([_measurement('urn:mavedb:test-a-1#1', relationship='exactMatch')], 'in no known way'),
        ([_measurement('urn:mavedb:test-a-1#1') | {'score': 'high'}], 'score in no known shape'),
        (
            [{'variantUrn': 'urn:mavedb:test-a-1#1', 'scoreSetUrn': 'urn:mavedb:test-a-1', 'relationship': 'direct'}],
            'does not state whether its score set version is current',
        ),
        (
            [_measurement('urn:mavedb:test-a-1#1') | {'isCurrent': 'true'}],
            'does not state whether its score set version is current',
        ),
    ],
)
def test_an_unreadable_measurement_list_is_never_mistaken_for_an_absence(listed: object, match: str) -> None:
    """A skipped-because-unparsable answer and a real absence would otherwise be the same NOT_FOUND.

    That answer is scored as "no assay covers this variant", so one renamed field would make the rpc
    report a finding about every variant it is asked.
    """
    handler = fake_mavedb.handler(
        measurements={'PA1': listed}, score_sets={'urn:mavedb:test-a-1': _score_set('urn:mavedb:test-a-1')}, variants={}
    )
    with pytest.raises(ValueError, match=match):
        _run(handler, lambda c: _fetch(c, 'PA1'))


@pytest.mark.parametrize(
    ('score_set', 'match'),
    [
        (
            {'urn': 'urn:mavedb:test-a-1', 'publishedDate': '2025-01-01', 'scoreCalibrations': {'primary': {}}},
            'scoreCalibrations in no known shape',
        ),
        (
            {'urn': 'urn:mavedb:test-a-1', 'publishedDate': '2025-01-01', 'scoreCalibrations': ['not an object']},
            'scoreCalibrations in no known shape',
        ),
        (
            {'urn': 'urn:mavedb:test-a-1', 'publishedDate': 20250101, 'scoreCalibrations': []},
            'publishedDate in no known shape',
        ),
    ],
)
def test_an_unreadable_score_set_is_never_read_as_an_uncalibrated_one(score_set: dict[str, object], match: str) -> None:
    handler = fake_mavedb.handler(
        measurements={'PA1': [_measurement('urn:mavedb:test-a-1#1')]},
        score_sets={'urn:mavedb:test-a-1': score_set},
        variants={},
    )
    with pytest.raises(ValueError, match=match):
        _run(handler, lambda c: _fetch(c, 'PA1'))


@pytest.mark.parametrize(
    'score_set',
    [
        {'urn': 'urn:mavedb:test-a-1', 'publishedDate': '2025-01-01', 'scoreCalibrations': None},
        {'urn': 'urn:mavedb:test-a-1', 'publishedDate': '2025-01-01'},
    ],
    ids=['null', 'absent'],
)
def test_a_score_set_stating_no_calibration_list_is_uncalibrated(score_set: dict[str, object]) -> None:
    """MaveDB's schema lets a score set state its calibrations as null or leave them out."""
    handler = _mavedb({'PA1': [_measurement('urn:mavedb:test-a-1#1', score=1.5)]}, score_set)
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.score == 1.5
    assert result.oddspath_ratio is None
    assert result.acmg_criterion == ''


_CLINVAR_CODING = {'id': 'clinvar:55407', 'system': 'https://www.ncbi.nlm.nih.gov/clinvar/variation/'}


@pytest.mark.parametrize(
    ('record', 'match'),
    [
        ({}, 'annotations in no known shape'),
        ({'annotations': [{'clinvar': [{'clinicalSignificance': 'Pathogenic'}]}]}, 'annotations in no known shape'),
        ({'annotations': {'ga4gh:VA.x': [{'clinicalSignificance': 'Pathogenic'}]}}, 'annotations in no known shape'),
        (
            {'annotations': {}, 'molecularRepresentation': [{'coding': _CLINVAR_CODING}]},
            'molecularRepresentation in no known shape',
        ),
        (
            {'annotations': {}, 'molecularRepresentation': {'mappings': {'clinvar': _CLINVAR_CODING}}},
            'cross-references in no known shape',
        ),
        (
            {'annotations': {}, 'molecularRepresentation': {'mappings': [_CLINVAR_CODING]}},
            'cross-reference in no known shape',
        ),
    ],
    ids=[
        'no annotations',
        'annotations as a list',
        'an allele annotated with a list',
        'a representation as a list',
        'mappings as an object',
        'a mapping with no coding',
    ],
)
def test_a_record_that_cannot_be_read_for_clinvar_entries_is_not_passed_through(
    record: dict[str, object], match: str
) -> None:
    """What cannot be read cannot be shown to hold no ClinVar entry."""
    handler = fake_mavedb.handler(
        measurements={'PA1': [_measurement('urn:mavedb:test-a-1#1')]},
        score_sets={'urn:mavedb:test-a-1': _score_set('urn:mavedb:test-a-1')},
        variants={'urn:mavedb:test-a-1#1': record},
    )
    with pytest.raises(ValueError, match=match):
        _run(handler, lambda c: _fetch(c, 'PA1'))


@pytest.mark.parametrize('representation', [None, {'type': 'CategoricalVariant'}], ids=['null', 'no mappings'])
def test_a_representation_with_no_cross_reference_is_passed_through_as_it_is(representation: object) -> None:
    """MaveDB states a null representation for a variant it could not map; there is nothing in it to leave out."""
    handler = fake_mavedb.handler(
        measurements={'PA1': [_measurement('urn:mavedb:test-a-1#1')]},
        score_sets={'urn:mavedb:test-a-1': _score_set('urn:mavedb:test-a-1')},
        variants={'urn:mavedb:test-a-1#1': {'annotations': {}, 'molecularRepresentation': representation}},
    )
    result = _run(handler, lambda c: _fetch(c, 'PA1'))
    assert result.raw['molecularRepresentation'] == representation


def _fetch_single_record(score: float | None, calibrations: list[dict[str, object]]) -> mavedb.MavedbResult:
    handler = _mavedb(
        {'PA1': [_measurement('urn:mavedb:cal-a-1#1', score=score)]},
        _score_set('urn:mavedb:cal-a-1', calibrations=calibrations),
    )
    return _run(handler, lambda c: _fetch(c, 'PA1'))


@pytest.mark.parametrize(
    ('score', 'expected_criterion', 'expected_strength', 'expected_oddspath'),
    [
        (-0.8, 'PS3', 'MODERATE', None),
        (-0.5, 'BS3', 'STRONG', 0.04),  # inclusive lower bound of the upper bin
    ],
)
def test_select_classification_bins_by_score(
    score: float,
    expected_criterion: str,
    expected_strength: str,
    expected_oddspath: float | None,
) -> None:
    calibrations: list[dict[str, object]] = [
        {
            'functionalClassifications': [
                {
                    'range': [None, -0.5],
                    'inclusiveUpperBound': False,
                    'acmgClassification': {'criterion': 'PS3', 'evidenceStrength': 'MODERATE'},
                },
                {
                    'range': [-0.5, None],
                    'inclusiveLowerBound': True,
                    'oddspathsRatio': 0.04,
                    'acmgClassification': {'criterion': 'BS3', 'evidenceStrength': 'STRONG'},
                },
            ]
        }
    ]
    result = _fetch_single_record(score, calibrations)
    assert result.acmg_criterion == expected_criterion
    assert result.acmg_strength == expected_strength
    assert result.oddspath_ratio == expected_oddspath


def test_select_classification_prefers_an_overlapping_bin_carrying_oddspath() -> None:
    calibrations: list[dict[str, object]] = [
        {
            'functionalClassifications': [
                {
                    'range': [None, None],
                    'acmgClassification': {'criterion': 'PS3', 'evidenceStrength': 'MODERATE'},
                },
                {
                    'range': [None, None],
                    'oddspathsRatio': 4.2,
                    'acmgClassification': {'criterion': 'PS3', 'evidenceStrength': 'STRONG'},
                },
            ]
        }
    ]
    result = _fetch_single_record(1.0, calibrations)
    assert result.oddspath_ratio == 4.2
    assert result.acmg_strength == 'STRONG'


def _calibration(title: str, odds: float, *, primary: bool | None, urn: str = 'urn:cal') -> dict[str, object]:
    return {
        'title': title,
        'urn': urn,
        'primary': primary,
        'functionalClassifications': [
            {'range': [None, None], 'oddspathsRatio': odds, 'acmgClassification': {'criterion': 'BS3'}}
        ],
    }


def test_the_depositors_primary_calibration_is_the_one_read() -> None:
    """A deposit carries several and they disagree; `primary` is the depositor's own answer."""
    calibrations = [
        _calibration('ExCALIBR', 9.9, primary=False, urn='urn:cal-a'),
        _calibration('Investigator-provided', 0.04, primary=True, urn='urn:cal-b'),
    ]
    result = _fetch_single_record(1.0, calibrations)
    assert result.oddspath_ratio == 0.04


def test_a_deposit_marking_no_primary_reads_them_in_an_order_the_payload_cannot_move() -> None:
    """Payload order would otherwise let serialisation pick an ACMG strength."""
    calibrations = [
        _calibration('later', 9.9, primary=False, urn='urn:cal-b'),
        _calibration('earlier', 0.04, primary=False, urn='urn:cal-a'),
    ]
    forward = _fetch_single_record(1.0, calibrations)
    reversed_order = _fetch_single_record(1.0, list(reversed(calibrations)))
    assert forward.oddspath_ratio == 0.04
    assert reversed_order.oddspath_ratio == 0.04


def test_a_deposit_marking_several_primary_fails_loudly() -> None:
    """There is no ground for choosing among them; guessing would restore the arbitrariness unlabelled."""
    calibrations = [
        _calibration('one', 9.9, primary=True, urn='urn:cal-a'),
        _calibration('two', 0.04, primary=True, urn='urn:cal-b'),
    ]
    with pytest.raises(ValueError, match='marks 2 calibrations primary'):
        _fetch_single_record(1.0, calibrations)


def test_select_classification_none_without_score() -> None:
    result = _fetch_single_record(None, [_open_bin(4.2)])
    assert result.score is None
    assert result.oddspath_ratio is None
    assert result.acmg_criterion == ''
