"""gnomAD GraphQL adapter: variant frequency + gene constraint, over a recorded payload.

The fetch paths are driven by an httpx2 `MockTransport` returning a committed gnomAD response
(variant `1-55051215-G-A` / gene `PCSK9`); no test hits the network. The gene payload's per-region
pext tissue lists are trimmed to four of gnomAD's 49 columns — the values are as recorded, the list
is shortened to keep the fixture readable.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import gnomad

_FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent / 'fixtures' / 'gnomad.json').read_bytes())
_VARIANT = _FIXTURE['variant']
_GENE = _FIXTURE['gene']


def _run[T](
    handler: Callable[[httpx2.Request], httpx2.Response], call: Callable[[httpx2.AsyncClient], Awaitable[T]]
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def test_fetch_gnomad_returns_the_variant_block_and_provenance() -> None:
    result: gnomad.GnomadResult = _run(
        lambda _r: httpx2.Response(200, json=_VARIANT),
        lambda c: gnomad.fetch_gnomad('1-55051215-G-A', 'gnomad_r4', http_client=c),
    )
    variant = result.raw['variant']
    assert isinstance(variant, dict)
    # the POP_FRQ / POP_HMZ inputs are reachable in raw
    assert variant['exome']['faf95']['popmax_population'] == 'sas'
    assert variant['exome']['homozygote_count'] == 1
    assert 'joint' in variant
    assert result.source == 'gnomAD GraphQL'
    assert result.dataset_versions == ('gnomad_r4',)
    assert '1-55051215-G-A' in result.query


def test_fetch_gnomad_omits_cooccurrence_when_not_requested() -> None:
    query: dict[str, str] = {}
    variables: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        query['q'] = body['query']
        variables.update(body['variables'])
        return httpx2.Response(200, json=_VARIANT)

    _run(handler, lambda c: gnomad.fetch_gnomad('1-55051215-G-A', 'gnomad_r4', http_client=c))
    assert 'variant_cooccurrence' not in query['q']
    assert 'b' not in variables


def test_fetch_gnomad_adds_cooccurrence_when_requested() -> None:
    query: dict[str, str] = {}
    variables: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        query['q'] = body['query']
        variables.update(body['variables'])
        return httpx2.Response(200, json=_VARIANT)

    result: gnomad.GnomadResult = _run(
        handler,
        lambda c: gnomad.fetch_gnomad(
            '13-32913055-A-G', 'gnomad_r2_1', http_client=c, cooccurrence_with='13-32912299-C-T'
        ),
    )
    assert 'variant_cooccurrence' in query['q']
    assert 'gnomad_r2_1' in query['q']
    assert variables['b'] == '13-32912299-C-T'
    assert '13-32912299-C-T' in result.query


def _gene_result() -> gnomad.GnomadGeneResult:
    return _run(
        lambda _r: httpx2.Response(200, json=_GENE),
        lambda c: gnomad.fetch_gnomad_gene('PCSK9', http_client=c),
    )


def test_fetch_gnomad_gene_parses_loeuf_and_pext() -> None:
    result = _gene_result()
    assert result.loeuf == _GENE['data']['gene']['gnomad_constraint']['oe_lof_upper']
    assert result.pext_regions
    first = result.pext_regions[0]
    assert (first.start, first.stop) == (55039838, 55040044)
    assert result.raw['symbol'] == 'PCSK9'
    assert result.dataset_versions == ('gnomad_r4',)


def test_the_mane_select_behind_the_pext_regions_is_read_in_both_namespaces() -> None:
    """The one RefSeq/Ensembl transcript pairing this upstream states, and the release pext is on."""
    mane = _gene_result().mane_select
    assert mane is not None
    assert (mane.refseq, mane.ensembl) == ('NM_174936.4', 'ENST00000302118.5')


def test_a_gene_without_a_mane_select_reports_none_rather_than_an_empty_pair() -> None:
    payload = copy.deepcopy(_GENE)
    payload['data']['gene']['mane_select_transcript'] = None
    result = _run(lambda _r: httpx2.Response(200, json=payload), lambda c: gnomad.fetch_gnomad_gene('X', http_client=c))
    assert result.mane_select is None


@pytest.mark.parametrize('missing', ['refseq_id', 'refseq_version', 'ensembl_id', 'ensembl_version'])
def test_a_partial_mane_select_pair_is_no_pair(missing: str) -> None:
    """Half a pair joins like a whole one wherever it is read, so it is not offered as one.

    Not raised either: every other signal this gene query carries is independent of the pairing, and
    the block stays in `raw` for a caller that wants to see what gnomAD stated.
    """
    payload = copy.deepcopy(_GENE)
    payload['data']['gene']['mane_select_transcript'][missing] = None
    result = _run(
        lambda _r: httpx2.Response(200, json=payload), lambda c: gnomad.fetch_gnomad_gene('PCSK9', http_client=c)
    )
    assert result.mane_select is None
    assert result.pext_regions  # the rest of the answer survives
    assert result.raw['mane_select_transcript'] is not None


def test_a_mane_select_block_that_is_not_an_object_raises() -> None:
    """A shape fault is not a gene without a MANE transcript."""
    payload = copy.deepcopy(_GENE)
    payload['data']['gene']['mane_select_transcript'] = 'NM_174936.4'
    with pytest.raises(ValueError, match='not an object'):
        _run(lambda _r: httpx2.Response(200, json=payload), lambda c: gnomad.fetch_gnomad_gene('PCSK9', http_client=c))


def test_pext_regions_carry_their_per_tissue_values() -> None:
    """The cross-tissue mean hides the tissue-specific case SM18 asks about."""
    region = _gene_result().pext_regions[0]
    assert set(region.tissues)  # a region with no tissue column answers no SM18 question
    # PCSK9 is a liver gene: this region is expressed there and in neither brain nor muscle, which
    # its 0.50 cross-tissue mean states as neither.
    assert region.tissues['liver'] > region.mean > region.tissues['brain_cortex']


@pytest.mark.parametrize(
    ('gtex_id', 'column'),
    [
        ('Muscle_Skeletal', 'muscle_skeletal'),
        ('Liver', 'liver'),
        ('Brain_Anterior_cingulate_cortex_BA24', 'brain_anterior_cingulate_cortex_ba24'),
        # the one GTEx spelling lower-casing alone does not reach
        ('Cells_EBV-transformed_lymphocytes', 'cells_ebv_transformed_lymphocytes'),
    ],
)
def test_a_gtex_tissue_id_maps_onto_gnomads_pext_column(gtex_id: str, column: str) -> None:
    """Two spellings of one tissue: the request carries GTEx's, gnomAD keys its pext columns by its own."""
    assert gnomad.pext_tissue_key(gtex_id) == column


def test_pext_regions_disagreeing_on_their_tissues_raise() -> None:
    """Per-exon values weight across regions, so a tissue present in only some averages a wrong span."""
    payload = copy.deepcopy(_GENE)
    payload['data']['gene']['pext']['regions'][0]['tissues'] = [{'tissue': 'liver', 'value': 0.5}]
    with pytest.raises(ValueError, match='different tissue vocabularies'):
        _run(lambda _r: httpx2.Response(200, json=payload), lambda c: gnomad.fetch_gnomad_gene('PCSK9', http_client=c))


@pytest.mark.parametrize('tissues', [None, []])
def test_a_pext_region_without_tissues_raises(tissues: list[object] | None) -> None:
    """Dropping or emptying the list would read back as "gnomAD carries no pext for that tissue"."""
    payload = copy.deepcopy(_GENE)
    if tissues is None:
        del payload['data']['gene']['pext']['regions'][0]['tissues']
    else:
        payload['data']['gene']['pext']['regions'][0]['tissues'] = tissues
    with pytest.raises(ValueError, match='carries no tissues'):
        _run(lambda _r: httpx2.Response(200, json=payload), lambda c: gnomad.fetch_gnomad_gene('PCSK9', http_client=c))


def test_fetch_gnomad_raises_on_absent_variant() -> None:
    with pytest.raises(errors.UnknownVariantError, match='holds no variant'):
        _run(
            lambda _r: httpx2.Response(
                200, json={'data': {'variant': None}, 'errors': [{'message': 'Variant not found'}]}
            ),
            lambda c: gnomad.fetch_gnomad('9-9-G-C', 'gnomad_r4', http_client=c),
        )


def test_an_id_gnomad_could_not_parse_is_not_an_absent_variant() -> None:
    """Both come back as a 200 with a null variant; only the reported message tells them apart.

    NOT_FOUND from this rpc IS the POP_FRQ rarity evidence, so collapsing the two would score a
    malformed id as "absent from gnomAD" — and never retry it, absence being a settled answer.
    """
    rejected = {'data': {'variant': None}, 'errors': [{'message': 'Invalid variant ID'}]}
    with pytest.raises(errors.InvalidRequestError, match='Invalid variant ID'):
        _run(
            lambda _r: httpx2.Response(200, json=rejected),
            lambda c: gnomad.fetch_gnomad('not-a-variant', 'gnomad_r4', http_client=c),
        )


def test_a_cooccurrence_sub_error_does_not_veto_the_absence_verdict() -> None:
    """GraphQL's errors array is top-level with a null `path`, so both root fields report into it.

    A variant absent from v2 with co-occurrence asked for yields the absence message beside the
    co-occurrence complaint. Requiring every message to state absence turned the POP_FRQ rarity
    evidence into INVALID_ARGUMENT — in the biallelic case gnomAD v2 exists for, where "rare variant
    absent from v2" is the ordinary answer.
    """
    both = {
        'data': {'variant': None, 'variant_cooccurrence': None},
        'errors': [
            {'message': 'Variant co-occurrence is only available for variants found in gnomAD'},
            {'message': 'Variant not found'},
        ],
    }
    with pytest.raises(errors.UnknownVariantError, match='holds no variant'):
        _run(
            lambda _r: httpx2.Response(200, json=both),
            lambda c: gnomad.fetch_gnomad(
                '17-31232881-G-C', 'gnomad_r2_1', http_client=c, cooccurrence_with='1-55039974-G-A'
            ),
        )


def test_a_refusal_anywhere_in_the_array_outranks_an_absence_beside_it() -> None:
    """`path: null` denies attribution, so a refusal may belong to either id in the query.

    A primary absent from v2 with an unparsable `cooccurrence_with` reports both messages. Settling
    that as "no record" answers the caller's typo'd second id as a rarity finding and never retries
    it — so a refusal anywhere wins, and only an array carrying none at all can settle an absence.
    Neither quantifier over the absence set alone gets both this and the co-occurrence case right.
    """
    mixed = {
        'data': {'variant': None, 'variant_cooccurrence': None},
        'errors': [{'message': 'Invalid variant ID'}, {'message': 'Variant not found'}],
    }
    with pytest.raises(errors.InvalidRequestError, match='did not accept'):
        _run(
            lambda _r: httpx2.Response(200, json=mixed),
            lambda c: gnomad.fetch_gnomad(
                '17-41223094-T-G', 'gnomad_r2_1', http_client=c, cooccurrence_with='99-31232881-G-C'
            ),
        )


def test_an_unexplained_null_field_is_neither_an_absence_nor_a_refusal() -> None:
    """With no message there is nothing to read; claiming absence would manufacture the evidence."""
    with pytest.raises(ValueError, match='malformed'):
        _run(
            lambda _r: httpx2.Response(200, json={'data': {'variant': None}}),
            lambda c: gnomad.fetch_gnomad('1-55051215-G-A', 'gnomad_r4', http_client=c),
        )


def test_fetch_gnomad_gene_raises_on_absent_gene() -> None:
    with pytest.raises(errors.UnknownVariantError, match='holds no gene'):
        _run(
            lambda _r: httpx2.Response(200, json={'data': {'gene': None}, 'errors': [{'message': 'Gene not found'}]}),
            lambda c: gnomad.fetch_gnomad_gene('NOTAGENE', http_client=c),
        )


def test_fetch_gnomad_raises_on_non_2xx() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _run(
            lambda _r: httpx2.Response(500, json={}),
            lambda c: gnomad.fetch_gnomad('1-55051215-G-A', 'gnomad_r4', http_client=c),
        )
