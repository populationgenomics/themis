"""MaveDB adapter: ClinGen allele lookup -> candidate selection -> calibration bin.

The happy path runs over a committed MaveDB payload — the four BRCA1 deposits that score the protein
allele `PA3057692902` (`NP_009225.1:p.Thr1677His`), trimmed of prose — so the candidate ordering and
the score-to-calibration binning are exercised against the shape the API really returns. No test hits
the network.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import mavedb

_FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent / 'fixtures' / 'mavedb.json').read_bytes())
_ALLELE_ID = _FIXTURE['clingen_allele_id']
_SCORED_URN = 'urn:mavedb:00001222-b-2#846'
_MISSES: dict[str, object] = {'exactMatch': None, 'equivalentNt': [], 'equivalentAa': []}


def _run[T](
    handler: Callable[[httpx2.Request], httpx2.Response], call: Callable[[httpx2.AsyncClient], Awaitable[T]]
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _variant_urn(path: str) -> str:
    """The URN a `/variants/{urn}` path names; httpx2 has already decoded the percent-encoding."""
    return path.rsplit('/', 1)[-1]


def _fixture_handler(seen: list[str] | None = None) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if request.method == 'POST' and path.endswith('/clingen-allele-id-lookups'):
            # MaveDB answers every id it was asked, an id it holds nothing under included.
            held = {e['clingenAlleleId']: e for e in _FIXTURE['lookup']}
            asked = json.loads(request.content)['clingenAlleleIds']
            return httpx2.Response(200, json=[held.get(a, _MISSES.copy() | {'clingenAlleleId': a}) for a in asked])
        if '/variants/' in path:
            record = _FIXTURE['variants'].get(_variant_urn(path))
            if record is None:
                return httpx2.Response(404, json={'detail': 'not found'})
            return httpx2.Response(200, json=record)
        raise AssertionError(f'unexpected request path {path!r}')

    return handler


def _entry(allele_id: str, kind: str, *deposits: tuple[str, str | None]) -> dict[str, object]:
    """One lookup answer reporting `deposits` under `kind`, shaped as MaveDB shapes it.

    Every kind is present: MaveDB reports a miss as an explicit null or empty list, never by leaving
    the key out.
    """
    variant = {
        'clingenAlleleId': allele_id,
        'variantEffectMeasurements': [
            {'urn': urn, 'scoreSet': {'urn': urn.split('#')[0], 'publishedDate': published}}
            for urn, published in deposits
        ],
    }
    return _MISSES.copy() | {
        'clingenAlleleId': allele_id,
        kind: variant if kind == 'exactMatch' else [variant],
    }


def _fetch(client: httpx2.AsyncClient, *allele_ids: str) -> Awaitable[mavedb.MavedbResult]:
    return mavedb.fetch_mavedb(allele_ids or (_ALLELE_ID,), http_client=client)


def test_fetch_mavedb_returns_the_scored_calibration() -> None:
    """The four committed deposits all score the allele; the most recently published one answers."""
    result = _run(_fixture_handler(), _fetch)

    assert result.score == _FIXTURE['expected_score']
    # the bin containing the score in the calibration the depositor marked primary
    assert result.oddspath_ratio == 0.0381
    assert result.acmg_criterion == 'BS3'
    assert result.acmg_strength == 'STRONG'
    assert result.source == 'MaveDB'
    assert result.dataset_versions == ('urn:mavedb:00001222-b-2',)
    assert isinstance(result.raw['scoreSet'], dict)


def test_no_candidate_is_read_twice_and_none_is_skipped() -> None:
    """Driven with nothing calibrated, so every candidate is read and the bound is not vacuous.

    Cost scales with what matched, never with a score set's size — a score-set-wide read is orders of
    magnitude larger than the caller's whole tool budget.
    """
    seen: list[str] = []
    uncalibrated = {
        urn: {**record, 'scoreSet': {**record['scoreSet'], 'scoreCalibrations': []}}
        for urn, record in _FIXTURE['variants'].items()
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.method == 'POST':
            return httpx2.Response(200, json=_FIXTURE['lookup'])
        return httpx2.Response(200, json=uncalibrated[_variant_urn(request.url.path)])

    _run(handler, _fetch)
    assert sum(1 for path in seen if path.endswith('/clingen-allele-id-lookups')) == 1
    reads = [_variant_urn(path) for path in seen if '/variants/urn' in path]
    assert sorted(reads) == sorted(c.urn for c in mavedb._candidates(_FIXTURE['lookup'], [_ALLELE_ID]))


def test_every_registered_allele_id_is_asked_in_one_request() -> None:
    """A nucleotide-level deposit and a protein-level one are keyed differently; both get asked."""
    asked: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == 'POST':
            asked.append(json.loads(request.content)['clingenAlleleIds'])
        return _fixture_handler()(request)

    _run(handler, lambda c: _fetch(c, 'CA000001', _ALLELE_ID))
    assert asked == [['CA000001', _ALLELE_ID]]


def test_a_variant_no_deposit_scores_is_a_settled_answer() -> None:
    """An answered-and-empty lookup is the only shape that means "no assay covers this variant"."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[_MISSES.copy() | {'clingenAlleleId': 'CA999'}])

    with pytest.raises(errors.UnknownVariantError, match='no scored variant'):
        _run(handler, lambda c: _fetch(c, 'CA999'))


def test_no_allele_id_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match='at least one ClinGen allele id'):
        _run(_fixture_handler(), lambda c: mavedb.fetch_mavedb([], http_client=c))


def test_non_2xx_raises() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _run(lambda _r: httpx2.Response(504, text='Gateway Time-out'), _fetch)


_UNCALIBRATED: dict[str, object] = {
    'urn': 'urn:mavedb:test-a-1#1',
    'data': {'score_data': {'score': 1.5}},
    'scoreSet': {'urn': 'urn:mavedb:test-a-1', 'publishedDate': '2025-01-01', 'scoreCalibrations': []},
}
_CALIBRATED: dict[str, object] = {
    'urn': 'urn:mavedb:test-b-1#1',
    'data': {'score_data': {'score': 1.5}},
    'scoreSet': {
        'urn': 'urn:mavedb:test-b-1',
        'publishedDate': '2025-01-01',
        'scoreCalibrations': [
            {'functionalClassifications': [{'range': [1.0, None], 'inclusiveLowerBound': True, 'oddspathsRatio': 4.2}]}
        ],
    },
}


def _published_date(record: dict[str, object]) -> str | None:
    score_set = record['scoreSet']
    assert isinstance(score_set, dict)
    return score_set.get('publishedDate')


def _two_candidate_handler(records: list[dict[str, object]]) -> Callable[[httpx2.Request], httpx2.Response]:
    by_urn = {record['urn']: record for record in records}
    lookup = [
        _entry(
            'PA1',
            'exactMatch',
            *((str(record['urn']), _published_date(record)) for record in records),
        )
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == 'POST':
            return httpx2.Response(200, json=lookup)
        return httpx2.Response(200, json=by_urn[_variant_urn(request.url.path)])

    return handler


def test_a_calibrated_deposit_is_preferred_over_an_uncalibrated_one() -> None:
    """Both score the allele; only one answers what the rpc is for, whichever order MaveDB lists them."""
    result = _run(_two_candidate_handler([_UNCALIBRATED, _CALIBRATED]), lambda c: _fetch(c, 'PA1'))
    assert result.dataset_versions == ('urn:mavedb:test-b-1',)
    assert result.oddspath_ratio == 4.2


def test_a_scored_deposit_is_preferred_over_one_carrying_no_score_at_all() -> None:
    """A record with neither a calibration nor a score would otherwise shadow one already fetched."""
    scoreless: dict[str, object] = {
        'urn': 'urn:mavedb:test-a-2#1',
        'data': {'score_data': {}},
        'scoreSet': {'urn': 'urn:mavedb:test-a-2', 'publishedDate': '2026-01-01', 'scoreCalibrations': []},
    }
    result = _run(_two_candidate_handler([scoreless, _UNCALIBRATED]), lambda c: _fetch(c, 'PA1'))
    assert result.score == 1.5
    assert result.dataset_versions == ('urn:mavedb:test-a-1',)


def test_an_uncalibrated_deposit_still_returns_its_score() -> None:
    """Absence of a calibration is the depositor's, not an absence of evidence: the score is the fact."""
    result = _run(_two_candidate_handler([_UNCALIBRATED]), lambda c: _fetch(c, 'PA1'))
    assert result.score == 1.5
    assert result.dataset_versions == ('urn:mavedb:test-a-1',)
    assert result.oddspath_ratio is None
    assert result.acmg_criterion == ''
    assert result.acmg_strength == ''


def test_every_candidate_considered_rides_in_the_provenance_query() -> None:
    result = _run(_fixture_handler(), _fetch)
    assert _ALLELE_ID in result.query
    assert result.query.count('urn:mavedb:') > 1  # the candidates, then the one selected
    assert result.query.endswith(_SCORED_URN)


@pytest.mark.parametrize('kind', ['exactMatch', 'equivalentAa', 'equivalentNt'])
def test_a_match_of_any_directness_is_a_candidate(kind: str) -> None:
    """MaveDB reports the allele itself and the ones it holds as equivalent; all name real deposits."""
    entry = _entry('PA1', kind, (str(_CALIBRATED['urn']), '2025-01-01'))
    assert [c.urn for c in mavedb._candidates([entry], ['PA1'])] == [_CALIBRATED['urn']]


def test_a_more_direct_match_outranks_an_equivalent_one() -> None:
    """An equivalent-nucleotide hit on a protein query is a different variant encoding the residue.

    It ranks below the exact one even though it is the more recent deposit.
    """
    entries = [
        _entry('PA1', 'equivalentNt', ('urn:mavedb:x-a-9', '2026-01-01')),
        _entry('PA2', 'exactMatch', ('urn:mavedb:x-a-1', '2020-01-01')),
    ]
    assert [c.urn for c in mavedb._candidates(entries, ['PA1', 'PA2'])] == ['urn:mavedb:x-a-1', 'urn:mavedb:x-a-9']


def test_the_more_recent_deposit_outranks_the_older_one() -> None:
    """Registration order is not evidential order: the accession that sorts first can be the 2021 one."""
    entries = [_entry('PA1', 'exactMatch', ('urn:mavedb:a-a-1#5', '2020-01-01'), ('urn:mavedb:z-a-1#5', '2026-01-01'))]
    assert [c.urn for c in mavedb._candidates(entries, ['PA1'])] == ['urn:mavedb:z-a-1#5', 'urn:mavedb:a-a-1#5']


def test_an_unpublished_deposit_ranks_below_every_published_one() -> None:
    """MaveDB reports an unpublished score set with a null date, which orders before every real one."""
    entries = [_entry('PA1', 'exactMatch', ('urn:mavedb:a-a-1', None), ('urn:mavedb:z-a-1', '2020-01-01'))]
    assert [c.urn for c in mavedb._candidates(entries, ['PA1'])] == ['urn:mavedb:z-a-1', 'urn:mavedb:a-a-1']


def test_the_callers_own_allele_outranks_a_derived_one_whatever_their_dates() -> None:
    """A coding expression registers the caller's change and, derived from it, its consequence.

    Both can reach a deposit as an exact match — of different things. The one keyed on the change the
    caller actually asked about answers, or a protein-level deposit published a day later would take
    over the coding form's answer, which is the form callers are told to send *because* it matches
    their nucleotide change.
    """
    entries = [
        _entry('CA1', 'exactMatch', ('urn:mavedb:nt-a-1', '2020-01-01')),
        _entry('PA1', 'exactMatch', ('urn:mavedb:aa-a-1', '2026-01-01')),
    ]
    assert [c.urn for c in mavedb._candidates(entries, ['CA1', 'PA1'])] == ['urn:mavedb:nt-a-1', 'urn:mavedb:aa-a-1']


def test_a_deposit_reached_under_two_alleles_is_credited_to_the_more_direct_one() -> None:
    """MaveDB labels a match against whichever id reached it, so one deposit arrives twice."""
    deposit = ('urn:mavedb:x-a-1', '2025-01-01')
    entries = [_entry('CA1', 'equivalentAa', deposit), _entry('PA1', 'exactMatch', deposit)]
    [candidate] = mavedb._candidates(entries, ['CA1', 'PA1'])
    assert (candidate.allele_id, candidate.kind) == ('PA1', 'exactMatch')


def test_a_dangling_variant_urn_is_the_upstreams_fault_not_the_callers() -> None:
    """The URN came from MaveDB's own lookup; blaming the request would send a caller rewriting it."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == 'POST':
            return httpx2.Response(200, json=[_entry(_ALLELE_ID, 'exactMatch', ('urn:mavedb:gone-a-1', '2025-01-01'))])
        return httpx2.Response(404, json={'detail': 'not found'})

    with pytest.raises(ValueError, match='which its own lookup named'):
        _run(handler, _fetch)


def test_a_repeated_allele_id_is_asked_once() -> None:
    """MaveDB echoes a repeated id twice, which would otherwise read as it answering the same id twice."""
    asked: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == 'POST':
            asked.append(json.loads(request.content)['clingenAlleleIds'])
        return _fixture_handler()(request)

    _run(handler, lambda c: _fetch(c, _ALLELE_ID, _ALLELE_ID))
    assert asked == [[_ALLELE_ID]]


def test_the_candidate_order_does_not_follow_the_payloads_own_order() -> None:
    """`oddspath_ratio` feeds the SVCv4 arithmetic, so two deposits must not answer by array order.

    The committed deposits disagree on strength for one score, so upstream ordering deciding the
    winner would make a scored field non-deterministic.
    """
    forward = mavedb._candidates(_FIXTURE['lookup'], [_ALLELE_ID])
    reversed_lookup = [
        {**entry, 'exactMatch': {**entry['exactMatch'], 'variantEffectMeasurements': list(reversed(vems))}}
        for entry in _FIXTURE['lookup']
        if (vems := entry['exactMatch']['variantEffectMeasurements'])
    ]
    assert [c.urn for c in mavedb._candidates(reversed_lookup, [_ALLELE_ID])] == [c.urn for c in forward]


def test_a_deposit_reached_by_two_ids_is_read_once() -> None:
    deposit = ('urn:mavedb:x-a-1', '2025-01-01')
    entries = [_entry('PA1', 'exactMatch', deposit), _entry('PA2', 'exactMatch', deposit)]
    assert [c.urn for c in mavedb._candidates(entries, ['PA1', 'PA2'])] == ['urn:mavedb:x-a-1']


@pytest.mark.parametrize(
    ('entries', 'asked', 'match'),
    [
        ([], ['PA1'], 'no answer for ClinGen allele'),
        ([_entry('PA1', 'exactMatch', ('u', '')), _entry('PA1', 'exactMatch', ('v', ''))], ['PA1'], 'answered twice'),
        ([{'clingenAlleleId': 'PA1', 'exactMatch': ['not an object']}], ['PA1'], 'no known shape'),
        ([{'clingenAlleleId': 'PA1', 'exactMatch': {'variantEffectMeasurements': {}}}], ['PA1'], 'no readable'),
        # A reported match is a match: zero measurements under it is a shape deviation, not an absence.
        ([{'clingenAlleleId': 'PA1', 'exactMatch': {'variantEffectMeasurements': []}}], ['PA1'], 'no readable'),
        ([{'clingenAlleleId': 'PA1', 'exactMatch': {'variantEffectMeasurements': [{}]}}], ['PA1'], 'naming no variant'),
        (
            [{'clingenAlleleId': 'PA1', 'exactMatch': {'variantEffectMeasurements': [{'urn': 'u'}]}}],
            ['PA1'],
            'names no score set',
        ),
        ([{'exactMatch': None}], ['PA1'], 'naming no ClinGen allele'),
    ],
)
def test_an_unreadable_answer_is_never_mistaken_for_an_absence(
    entries: list[dict[str, object]], asked: list[str], match: str
) -> None:
    """A skipped-because-unparsable answer and a real absence would otherwise be the same NOT_FOUND.

    That answer is scored as "no assay covers this variant", so one renamed field would make the rpc
    report a finding about every variant it is asked.
    """
    with pytest.raises(ValueError, match=match):
        mavedb._candidates(entries, asked)


def test_select_classification_bins_by_score_and_prefers_oddspath() -> None:
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
    below = mavedb._select_classification(calibrations, -0.8)
    assert below is not None
    below_acmg = below['acmgClassification']
    assert isinstance(below_acmg, dict)
    assert below_acmg['criterion'] == 'PS3'
    at_boundary = mavedb._select_classification(calibrations, -0.5)  # inclusive lower bound of the upper bin
    assert at_boundary is not None
    assert at_boundary['oddspathsRatio'] == 0.04


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
    selected = mavedb._select_classification(calibrations, 1.0)
    assert selected is not None
    assert selected['oddspathsRatio'] == 0.04


def test_a_deposit_marking_no_primary_reads_them_in_an_order_the_payload_cannot_move() -> None:
    """Payload order would otherwise let serialisation pick an ACMG strength."""
    calibrations = [
        _calibration('later', 9.9, primary=False, urn='urn:cal-b'),
        _calibration('earlier', 0.04, primary=False, urn='urn:cal-a'),
    ]
    forward = mavedb._select_classification(calibrations, 1.0)
    reversed_order = mavedb._select_classification(list(reversed(calibrations)), 1.0)
    assert forward is not None
    assert forward['oddspathsRatio'] == 0.04
    assert reversed_order == forward


def test_a_deposit_marking_several_primary_fails_loudly() -> None:
    """There is no ground for choosing among them; guessing would restore the arbitrariness unlabelled."""
    calibrations = [
        _calibration('one', 9.9, primary=True, urn='urn:cal-a'),
        _calibration('two', 0.04, primary=True, urn='urn:cal-b'),
    ]
    with pytest.raises(ValueError, match='marks 2 calibrations primary'):
        mavedb._select_classification(calibrations, 1.0)


def test_select_classification_none_without_score() -> None:
    assert mavedb._select_classification([{'functionalClassifications': []}], None) is None
