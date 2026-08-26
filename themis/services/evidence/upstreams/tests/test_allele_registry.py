"""Tests for the ClinGen Allele Registry adapter, against a recorded fixture via a mocked transport."""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import allele_registry

_FIXTURE = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'allele_registry.json').read_text())


def _fetch(handler: httpx.MockTransport) -> allele_registry.AlleleRegistryResult:
    async def run() -> allele_registry.AlleleRegistryResult:
        async with httpx.AsyncClient(transport=handler) as client:
            return await allele_registry.fetch_allele_registry('NM_000546.6:c.524G>A', http_client=client)

    return asyncio.run(run())


def test_happy_path_parses_caid_crossids_and_mane_projection() -> None:
    result = _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json=_FIXTURE)))

    assert result.caid == 'CA000251'
    assert result.gnomad_v4_id == '17-7675088-C-T'
    assert result.gnomad_v2_id == '17-7578406-C-T'
    assert result.gene == 'TP53'
    mane = [t for t in result.transcripts if t.mane_select]
    assert any(t.transcript == 'NM_000546.6' and t.hgvs_c == 'NM_000546.6:c.524G>A' for t in mane)
    # The registry lists the MANE Select transcript once per namespace, Ensembl first, so this must
    # come from the MANE RefSeq pairing rather than from whichever projection leads the list.
    assert mane[0].transcript.startswith('ENST')
    assert result.canonical_refseq_hgvs == 'NM_000546.6:c.524G>A'
    assert result.source == 'ClinGen Allele Registry'
    assert result.raw['@id'] == 'http://reg.genome.network/allele/CA000251'
    assert 'hgvs=' in result.query


def test_upstream_failure_raises_http_status_error_carrying_the_detail() -> None:
    body = {'errorType': 'InternalServerError', 'message': 'coordinates outside reference'}
    with pytest.raises(httpx.HTTPStatusError, match='coordinates outside reference'):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(500, json=body)))


def test_rejected_hgvs_surfaces_the_registry_s_own_reason() -> None:
    """A 4xx is the registry judging our input; its reason is the only thing that says what to fix."""
    body = {'errorType': 'HgvsParsingError', 'description': 'Given HGVS expressions cannot be parsed.'}
    with pytest.raises(errors.InvalidRequestError, match=r'HgvsParsingError.*cannot be parsed'):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(400, json=body)))


def test_payload_without_caid_raises_value_error() -> None:
    with pytest.raises(ValueError, match='no @id'):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json={'transcriptAlleles': []})))


def test_mane_plus_clinical_carries_the_canonical_when_there_is_no_mane_select() -> None:
    """A gene whose clinically-used transcript is Plus Clinical still canonicalises, not falls through."""
    mane = {
        'maneStatus': 'MANE Plus Clinical',
        'nucleotide': {'Ensembl': {'hgvs': 'ENST00000001.1:c.1A>T'}, 'RefSeq': {'hgvs': 'NM_000001.1:c.1A>T'}},
    }
    payload = {
        '@id': 'http://reg.genome.network/allele/CA1',
        'transcriptAlleles': [
            {'geneSymbol': 'X', 'hgvs': ['ENST00000001.1:c.1A>T'], 'MANE': mane},
            {'geneSymbol': 'X', 'hgvs': ['NM_000001.1:c.1A>T'], 'MANE': mane},
        ],
    }
    result = _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))

    assert result.canonical_refseq_hgvs == 'NM_000001.1:c.1A>T'


def test_allele_without_a_mane_select_transcript_has_no_refseq_canonical() -> None:
    payload = {
        '@id': 'http://reg.genome.network/allele/CA1',
        'transcriptAlleles': [{'geneSymbol': 'X', 'hgvs': ['NM_1.1:c.1A>T']}],
    }
    result = _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))

    assert result.canonical_refseq_hgvs is None


def _fetch_ids(payload: object, hgvs: str = 'NM_000546.6:c.524G>A') -> allele_registry.ClinGenAlleleIds:
    async def run() -> allele_registry.ClinGenAlleleIds:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            return await allele_registry.fetch_clingen_allele_ids(hgvs, http_client=client)

    return asyncio.run(run())


def test_a_coding_expression_registers_its_canonical_allele_and_its_protein_alleles() -> None:
    """A source keyed on either kind is reachable from one lookup, which is why both are returned."""
    result = _fetch_ids(_FIXTURE)

    assert result.allele_ids[0] == 'CA000251'  # the expression names the nucleotide change itself
    assert 'PA106629' in result.allele_ids  # its protein consequence, registered separately
    assert result.source == 'ClinGen Allele Registry'


def test_a_protein_expression_registers_a_protein_allele_alone() -> None:
    payload = {'@id': 'http://reg.genome.network/allele/PA106629', 'aminoAcidAlleles': [{'geneSymbol': 'TP53'}]}
    assert _fetch_ids(payload, 'NP_000537.3:p.Arg175His').allele_ids == ['PA106629']


def test_the_mane_transcripts_protein_allele_leads_the_others() -> None:
    """Several transcripts carry the same change under different protein alleles; MANE is asked first."""
    payload = {
        '@id': 'http://reg.genome.network/allele/CA1',
        'transcriptAlleles': [
            {'@id': 'http://reg.genome.network/allele/PA3', 'hgvs': ['NM_3.1:c.1A>T']},
            {
                '@id': 'http://reg.genome.network/allele/PA1',
                'hgvs': ['NM_1.1:c.1A>T'],
                'MANE': {'maneStatus': 'MANE Select'},
            },
            {
                '@id': 'http://reg.genome.network/allele/PA2',
                'hgvs': ['NM_2.1:c.1A>T'],
                'MANE': {'maneStatus': 'MANE Plus Clinical'},
            },
        ],
    }
    assert _fetch_ids(payload).allele_ids == ['CA1', 'PA1', 'PA2', 'PA3']


def test_one_protein_allele_spanning_transcript_versions_is_asked_once() -> None:
    payload = {
        '@id': 'http://reg.genome.network/allele/CA1',
        'transcriptAlleles': [
            {'@id': 'http://reg.genome.network/allele/PA1', 'hgvs': ['NM_1.1:c.1A>T']},
            {'@id': 'http://reg.genome.network/allele/PA1', 'hgvs': ['NM_1.2:c.1A>T']},
        ],
    }
    assert _fetch_ids(payload).allele_ids == ['CA1', 'PA1']


def test_an_unregistered_allele_is_a_verdict_on_the_expression_not_a_fault() -> None:
    """The registry answers an expression it registers no allele for with a blank node, not an error.

    Re-asking cannot change that, so it must not reach a caller as the uncharacterised fault its
    retry helper reissues — nor as the source holding no record of a variant it was never asked about.
    """
    with pytest.raises(errors.InvalidRequestError, match='registers no ClinGen allele'):
        _fetch_ids({'@id': '_:PA', 'aminoAcidAlleles': [{'geneSymbol': 'TP53'}]}, 'ENSP00000269305.4:p.Arg175His')


@pytest.mark.parametrize(
    ('payload', 'match'),
    [
        ({'transcriptAlleles': []}, 'no @id'),
        ({'@id': 'http://reg.genome.network/allele/CA1', 'transcriptAlleles': {}}, 'not a list'),
        ({'@id': 'http://reg.genome.network/allele/CA1', 'transcriptAlleles': ['x']}, 'non-object entry'),
        ({'@id': 'http://reg.genome.network/allele/CA1', 'transcriptAlleles': [{'@id': 7}]}, 'not an IRI'),
        (
            {'@id': 'http://reg.genome.network/allele/CA1', 'transcriptAlleles': [{'@id': 'http://x/allele/Q1'}]},
            'neither a registered allele nor a blank node',
        ),
    ],
)
def test_a_record_shape_this_cannot_read_is_never_read_as_fewer_alleles(payload: dict[str, object], match: str) -> None:
    """Dropping a protein allele here is invisible: the lookup keyed on it then answers nothing.

    That reads back as "no assay covers this variant" — a scored input — for every variant whose
    deposits are protein-keyed, which is most of them.
    """
    with pytest.raises(ValueError, match=match):
        _fetch_ids(payload)


def test_a_transcript_carrying_no_allele_id_is_not_a_deviation() -> None:
    """The registry registers ids for RefSeq transcripts and leaves Ensembl ones without."""
    payload = {
        '@id': 'http://reg.genome.network/allele/CA1',
        'transcriptAlleles': [
            {'hgvs': ['ENST00000001.1:c.1A>T']},
            {'@id': '_:PA', 'hgvs': ['XM_000001.1:c.1A>T']},
            {'@id': 'http://reg.genome.network/allele/PA1', 'hgvs': ['NM_000001.1:c.1A>T']},
        ],
    }
    assert _fetch_ids(payload).allele_ids == ['CA1', 'PA1']


def _parsed(payload: object) -> allele_registry.AlleleRegistryResult:
    return _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))


def test_the_clinvar_crosswalk_names_the_records_the_canonical_allele_resolves_to() -> None:
    """The route ClinVar's own index cannot give: it holds renderings, and this holds identities."""
    parsed = _parsed(_FIXTURE)

    variation = parsed.clinvar_variations[0]
    assert (variation.variation_id, variation.vcv) == (12374, 'VCV000012374')
    assert 'RCV000013173' in variation.rcv
    allele = parsed.clinvar_alleles[0]
    assert (allele.allele_id, allele.preferred_name) == (27413, 'NM_000546.6(TP53):c.524G>A (p.Arg175His)')


def test_the_accession_is_padded_to_the_width_efetch_resolves() -> None:
    """A bare UID efetches to an empty result set, so the padding is part of the identifier."""
    entry = {'variationId': 704508}
    assert _parsed({**_FIXTURE, 'externalRecords': {'ClinVarVariations': [entry]}}).clinvar_variations[0].vcv == (
        'VCV000704508'
    )


def test_a_variation_id_wider_than_the_accession_raises_rather_than_overflowing_it() -> None:
    """The rpcs that take a VCV hold it to that width, so an over-wide one is unaskable."""
    with pytest.raises(ValueError, match='does not fit'):
        _parsed({**_FIXTURE, 'externalRecords': {'ClinVarVariations': [{'variationId': 1234567890}]}})


@pytest.mark.parametrize(
    'external',
    [
        {'dbSNP': _FIXTURE['externalRecords']['dbSNP']},
        {},
    ],
    ids=['other-sources-only', 'no-external-records'],
)
def test_an_allele_the_registry_crosswalks_to_no_clinvar_record_is_an_answer(external: object) -> None:
    """This is what makes "ClinVar holds no record" statable rather than inferred from a search miss."""
    parsed = _parsed({**_FIXTURE, 'externalRecords': external})
    assert parsed.clinvar_variations == []
    assert parsed.clinvar_alleles == []


def test_an_expression_the_registry_registers_no_allele_for_establishes_no_absence() -> None:
    """A blank node is the registry declining the expression, not a record with an empty crosswalk.

    Read as a record it would become the finding that ClinVar holds none, off a payload that never
    said so — and read as a fault it would be retried against a settled answer.
    """
    with pytest.raises(allele_registry.UnregisteredAlleleError, match='no canonical allele'):
        _parsed({**_FIXTURE, '@id': '_:CA'})


@pytest.mark.parametrize('external', ['x', ['ClinVarVariations'], 7])
def test_a_cross_source_block_this_cannot_read_is_never_read_as_no_record(external: object) -> None:
    """Coerced to an empty object it would answer with no ClinVar crosswalk and no gnomAD id."""
    with pytest.raises(ValueError, match='externalRecords is not an object'):
        _parsed({**_FIXTURE, 'externalRecords': external})


@pytest.mark.parametrize(
    ('key', 'entries', 'match'),
    [
        ('ClinVarVariations', 'not-a-list', 'not a list'),
        ('ClinVarVariations', [['nested']], 'non-object entry'),
        ('ClinVarVariations', [{'@id': 'http://x/clinvar/variation/12374'}], 'no integer variationId'),
        ('ClinVarVariations', [{'variationId': '12374'}], 'no integer variationId'),
        ('ClinVarVariations', [{'variationId': 1, 'RCV': 'RCV1'}], 'RCV is not a list'),
        ('ClinVarVariations', [{'variationId': 1, 'RCV': [7]}], 'non-string entry'),
        ('ClinVarAlleles', [{'alleleId': 1}], 'no preferredName'),
        ('ClinVarAlleles', [{'preferredName': 'n'}], 'no integer alleleId'),
    ],
)
def test_a_crosswalk_shape_this_cannot_read_is_never_read_as_no_record(key: str, entries: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _parsed({**_FIXTURE, 'externalRecords': {key: entries}})


def test_a_variation_listed_twice_is_named_once() -> None:
    entry = _FIXTURE['externalRecords']['ClinVarVariations'][0]
    parsed = _parsed({**_FIXTURE, 'externalRecords': {'ClinVarVariations': [entry, entry]}})
    assert [v.variation_id for v in parsed.clinvar_variations] == [12374]
