"""Tests for the Broad SpliceAI + Pangolin adapter, against recorded fixtures via a mocked transport."""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import spliceai

_FIXTURE = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'spliceai.json').read_text())


def _route(spliceai_response: httpx2.Response, pangolin_response: httpx2.Response) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if '/spliceai/' in request.url.path:
            return spliceai_response
        if '/pangolin/' in request.url.path:
            return pangolin_response
        return httpx2.Response(404)

    return httpx2.MockTransport(handler)


def _fetch(handler: httpx2.MockTransport, variant: str = '8-140300616-T-G') -> spliceai.SpliceResult:
    async def run() -> spliceai.SpliceResult:
        async with httpx2.AsyncClient(transport=handler) as client:
            return await spliceai.fetch_splice(variant, 'GRCh38', http_client=client)

    return asyncio.run(run())


def test_happy_path_computes_maxima_and_normalises_chr_prefix() -> None:
    result = _fetch(
        _route(
            httpx2.Response(200, json=_FIXTURE['spliceai']),
            httpx2.Response(200, json=_FIXTURE['pangolin']),
        )
    )

    assert result.spliceai_gain == 0.04
    assert result.spliceai_loss == 0.83
    assert result.pangolin_gain == 0.29
    assert result.pangolin_loss == 0.85
    assert result.raw['spliceai'] == _FIXTURE['spliceai']
    assert result.raw['pangolin'] == _FIXTURE['pangolin']
    assert result.dataset_versions == ('GRCh38',)
    assert 'chr8-140300616-T-G' in result.query


def test_gain_and_loss_are_reported_apart_under_pangolins_negative_loss() -> None:
    """A Pangolin loss must not be masked by a co-reported gain: they are separate deltas.

    Pangolin signs a loss negative, so the two directions are not comparable as one maximum — the
    donor-loss variant below has a near-zero gain and a strong loss.
    """
    donor_loss = {'DS_AG': '0.00', 'DS_AL': '0.00', 'DS_DG': '0.00', 'DS_DL': '0.76'}
    result = _fetch(
        _route(
            httpx2.Response(200, json={'scores': [donor_loss]}),
            httpx2.Response(200, json={'scores': [{'DS_SG': '0.01', 'DS_SL': '-0.71'}]}),
        )
    )

    assert result.spliceai_loss == 0.76
    assert result.spliceai_gain == 0.00
    assert result.pangolin_loss == 0.71
    assert result.pangolin_gain == 0.01


def test_maxima_are_taken_across_scored_transcripts() -> None:
    spliceai_scores = [
        {'DS_AG': '0.10', 'DS_AL': '0.20', 'DS_DG': '0.30', 'DS_DL': '0.40'},
        {'DS_AG': '0.50', 'DS_AL': '0.60', 'DS_DG': '0.05', 'DS_DL': '0.15'},
    ]
    pangolin_scores = [{'DS_SG': '0.10', 'DS_SL': '-0.20'}, {'DS_SG': '0.30', 'DS_SL': '-0.05'}]
    result = _fetch(
        _route(
            httpx2.Response(200, json={'scores': spliceai_scores}),
            httpx2.Response(200, json={'scores': pangolin_scores}),
        )
    )

    assert result.spliceai_gain == 0.50
    assert result.spliceai_loss == 0.60
    assert result.pangolin_gain == 0.30
    assert result.pangolin_loss == 0.20


@pytest.mark.parametrize(
    ('spliceai_score', 'pangolin_score'),
    [
        ({'DS_AL': '-0.40'}, {'DS_SG': '0.01', 'DS_SL': '-0.71'}),
        ({'DS_AL': '0.40'}, {'DS_SG': '0.01', 'DS_SL': '0.71'}),
    ],
)
def test_a_delta_against_its_predictors_sign_convention_raises(
    spliceai_score: dict[str, str], pangolin_score: dict[str, str]
) -> None:
    """Orienting the deltas rests on the conventions; a payload that broke one must not be reoriented.

    Negating a magnitude would turn a strong predicted loss into a strong-looking absence — the
    silent wrong answer the split exists to prevent.
    """
    with pytest.raises(ValueError, match='sign convention'):
        _fetch(
            _route(
                httpx2.Response(200, json={'scores': [spliceai_score]}),
                httpx2.Response(200, json={'scores': [pangolin_score]}),
            )
        )


def test_empty_scores_yield_none() -> None:
    result = _fetch(
        _route(
            httpx2.Response(200, json={'scores': []}),
            httpx2.Response(200, json={'scores': []}),
        )
    )

    assert result.spliceai_gain is None
    assert result.spliceai_loss is None
    assert result.pangolin_gain is None
    assert result.pangolin_loss is None


def test_non_2xx_raises_http_status_error() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _fetch(_route(httpx2.Response(500, json={}), httpx2.Response(200, json=_FIXTURE['pangolin'])))


@pytest.mark.parametrize(
    'reported',
    [
        'The SpliceAI model did not return any scores for this variant',
        'Pangolin was unable to compute scores for this variant',
    ],
)
def test_a_position_the_model_could_not_score_is_an_absent_variant(reported: str) -> None:
    with pytest.raises(errors.UnknownVariantError, match='no scores'):
        _fetch(_route(httpx2.Response(200, json={'error': reported}), httpx2.Response(200, json=_FIXTURE['pangolin'])))


def _routed(predictor: str, failing: httpx2.Response) -> httpx2.MockTransport:
    """`failing` in ``predictor``'s slot, the recorded fixture in the other's."""
    scored = {name: httpx2.Response(200, json=_FIXTURE[name]) for name in ('spliceai', 'pangolin')}
    scored[predictor] = failing
    return _route(scored['spliceai'], scored['pangolin'])


@pytest.mark.parametrize('predictor', ['spliceai', 'pangolin'])
def test_an_id_the_service_could_not_parse_is_not_an_unscorable_position(predictor: str) -> None:
    """Both verdicts arrive inside a 200 and differ only in the `error` string.

    Reading them alike returns a caller's typo as "this position is unscorable" — the SPL_PRD
    finding — and never retries it, an unscorable position being settled. A servicer-side shape check
    cannot stand in: it is calibrated to gnomAD, which parses casings the Broad services reject.
    """
    unparsable = httpx2.Response(200, json={'error': 'Unable to parse variant: chr17-43045677-a-c'})
    with pytest.raises(errors.InvalidRequestError, match='could not parse'):
        _fetch(_routed(predictor, unparsable))


@pytest.mark.parametrize('predictor', ['spliceai', 'pangolin'])
@pytest.mark.parametrize(
    'payload',
    [
        {'error': 'Internal server error while loading the annotation'},
        {'error': {'code': 500}},  # not a string
        {'variant': 'chr17-31232881-G-C', 'hg': '38'},  # no `error` key at all
        {},
    ],
)
def test_a_payload_this_adapter_cannot_place_is_not_an_unscorable_position(
    predictor: str, payload: dict[str, object]
) -> None:
    """The default has to be the non-evidence branch, mirroring the gnomAD adapter.

    Matching only the caller's-fault string and letting everything else fall through would make an
    internal error, a missing key, and any future rewording of the unscorable message all read as
    the SPL_PRD finding — silently, and without a retry.
    """
    with pytest.raises(ValueError, match=r'neither scores nor an error|cannot place'):
        _fetch(_routed(predictor, httpx2.Response(200, json=payload)))
