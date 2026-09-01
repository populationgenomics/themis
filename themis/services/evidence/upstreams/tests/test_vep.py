"""Tests for the Ensembl VEP adapter: SO-term mapping + payload parsing via a mocked transport."""

from __future__ import annotations

import asyncio
import json
import pathlib
import urllib.parse
from collections.abc import Callable, Sequence

import httpx2
import pytest

from themis.evidence.models import evidence_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import vep

_FIXTURE = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'vep.json').read_text())
_VARIANT = 'NM_001042492.3:c.3496G>C'
_SOFTWARE_PATH = '/info/software'
_RELEASE = 116

# Each per-transcript field the rpc's contract promises, under the VEP REST option that produces it
# (verified against the live API). Written out here rather than read off the adapter, so a dropped
# option fails instead of agreeing with itself; the pairing is what the fixture then checks, since
# Ensembl answers a misspelled option with a 200 that merely lacks the field.
_OPTION_FIELDS = {
    'hgvs': ('hgvsc', 'hgvsp'),
    'numbers': ('exon',),
    'canonical': ('canonical',),
    'mane': ('mane', 'mane_select'),
}

# Each accepted predictor's output field, stated here for the same reason: an unrecognised flag is
# ignored inside a 200, so only a payload recorded against the wire form the adapter builds tells a
# working form from one whose score is silently absent. The fixture is that recording.
_PREDICTOR_FIELDS = {
    'AlphaMissense': 'alphamissense',
    'BayesDel': 'bayesdel_noaf_score',
    'CADD': 'cadd_phred',
    'ESM1b': 'esm1b_score',
    'MutPred2': 'mutpred2_score',
    'REVEL': 'revel',
    'SpliceAI': 'spliceai',
    'VARITY_R': 'varity_r_score',
}


def _annotating(annotate: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.MockTransport:
    """A transport answering the release call, and every other request through ``annotate``.

    Each `fetch_vep` issues two requests: the annotation, and the `/info/software` release that
    stamps its provenance. Only the first is ever what a test is about.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _SOFTWARE_PATH:
            return httpx2.Response(200, json={'release': _RELEASE})
        return annotate(request)

    return httpx2.MockTransport(handler)


def _issued_request(handler_variant: str, predictors: Sequence[str] = ()) -> httpx2.Request:
    """The annotation request `fetch_vep` puts on the wire for ``handler_variant``."""
    seen: list[httpx2.Request] = []

    def annotate(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_FIXTURE)

    async def run() -> vep.VepResult:
        async with httpx2.AsyncClient(transport=_annotating(annotate)) as client:
            return await vep.fetch_vep(handler_variant, predictors, 'GRCh38', http_client=client)

    asyncio.run(run())
    return seen[0]


def _requested_expression(handler_variant: str) -> str:
    """The HGVS expression `fetch_vep` puts on the wire for ``handler_variant``, decoded."""
    path = _issued_request(handler_variant).url.path
    return urllib.parse.unquote(path).removeprefix('/vep/human/hgvs/')


def _fetch(
    annotate: Callable[[httpx2.Request], httpx2.Response], predictors: Sequence[str] = ('AlphaMissense', 'BayesDel')
) -> vep.VepResult:
    async def run() -> vep.VepResult:
        async with httpx2.AsyncClient(transport=_annotating(annotate)) as client:
            return await vep.fetch_vep(_VARIANT, predictors, 'GRCh38', http_client=client)

    return asyncio.run(run())


def test_every_promised_per_transcript_field_is_asked_for() -> None:
    """VEP emits none of them by default, and a request naming no predictor still promises them."""
    params = _issued_request(_VARIANT).url.params
    assert set(_OPTION_FIELDS) <= set(params)


def test_the_recorded_payload_carries_what_those_options_produce() -> None:
    """The fixture is the live answer to exactly this option set — what makes the names falsifiable.

    An option Ensembl does not recognise is ignored inside a 200, so no response shape rules a
    misspelling out; only a payload recorded against the option set does.
    """
    returned = {field for consequence in _FIXTURE[0]['transcript_consequences'] for field in consequence}
    assert {field for fields in _OPTION_FIELDS.values() for field in fields} <= returned


def test_the_recorded_payload_carries_every_accepted_predictors_score() -> None:
    """Recorded against the wire forms the adapter builds for the whole accepted set.

    `?BayesDel=1` answers 200 with the score absent, so a wrong form is invisible in the response
    shape; this is what tells the form that works from the one that reads as "no score".
    """
    assert set(_PREDICTOR_FIELDS) == vep.ACCEPTED_PREDICTORS
    returned = {field for consequence in _FIXTURE[0]['transcript_consequences'] for field in consequence}
    assert set(_PREDICTOR_FIELDS.values()) <= returned


def test_a_first_class_predictor_rides_alongside_the_per_transcript_options() -> None:
    params = _issued_request(_VARIANT, ['AlphaMissense', 'REVEL']).url.params
    assert set(_OPTION_FIELDS) | {'AlphaMissense', 'REVEL'} <= set(params)


def test_a_dbnsfp_predictor_names_its_column_rather_than_becoming_a_flag() -> None:
    """A flag is what does NOT work: `?BayesDel=1` is a 200 with the score silently absent."""
    params = _issued_request(_VARIANT, ['BayesDel', 'ESM1b']).url.params

    assert 'BayesDel' not in params
    assert params['dbNSFP'] == 'transcript_match=1,BayesDel_noAF_score,ESM1b_score'


def test_the_two_wire_forms_ride_on_one_request() -> None:
    params = _issued_request(_VARIANT, ['BayesDel', 'AlphaMissense']).url.params

    assert params['AlphaMissense'] == '1'
    assert params['dbNSFP'] == 'transcript_match=1,BayesDel_noAF_score'


def test_a_request_naming_no_dbnsfp_predictor_sends_no_plugin_parameter() -> None:
    assert 'dbNSFP' not in _issued_request(_VARIANT, ['AlphaMissense']).url.params


@pytest.mark.parametrize('predictor', ['BayesDel_noAF', 'bayesdel', 'VEST4', 'PolyPhen-2', 'LoFtool'])
def test_a_predictor_with_no_wire_form_is_refused_rather_than_sent(predictor: str) -> None:
    """Unsent it is a request that was never made; sent it is a 200 that reads as "no score"."""
    with pytest.raises(errors.InvalidRequestError, match='takes predictors from'):
        _issued_request(_VARIANT, ['AlphaMissense', predictor])


def test_happy_path_maps_consequence_and_keeps_raw() -> None:
    result = _fetch(lambda _: httpx2.Response(200, json=_FIXTURE))

    assert result.most_severe_consequence == evidence_pb2.CONSEQUENCE_MISSENSE
    assert result.dataset_versions == (f'VEP {_RELEASE}', 'GRCh38')
    assert result.raw['most_severe_consequence'] == 'missense_variant'
    assert 'transcript_consequences' in result.raw


def test_a_host_that_states_no_release_is_a_fault_not_a_bare_assembly() -> None:
    """A stamp naming the assembly alone reads as a full one, so there is no partial to fall back to."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={} if request.url.path == _SOFTWARE_PATH else _FIXTURE)

    async def run() -> vep.VepResult:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await vep.fetch_vep(_VARIANT, [], 'GRCh38', http_client=client)

    with pytest.raises(ValueError, match='no integer release'):
        asyncio.run(run())


def test_gene_identity_from_canonical_consequence() -> None:
    result = _fetch(lambda _: httpx2.Response(200, json=_FIXTURE))

    assert result.gene_symbol == 'NF1'
    assert result.hgnc_id == 'HGNC:7765'


def test_gene_identity_prefers_flagged_canonical_transcript() -> None:
    annotation = {
        'most_severe_consequence': 'missense_variant',
        'transcript_consequences': [
            {'gene_symbol': 'OTHER', 'hgnc_id': 'HGNC:999', 'consequence_terms': ['missense_variant']},
            {
                'gene_symbol': 'NF1',
                'hgnc_id': 'HGNC:7765',
                'mane_select': 'NM_001042492.3',
                'consequence_terms': ['missense_variant'],
            },
        ],
    }
    result = vep.parse_vep(annotation, dataset_versions=(f'VEP {_RELEASE}', 'GRCh38'), query='q', dbnsfp_columns=())
    assert (result.gene_symbol, result.hgnc_id) == ('NF1', 'HGNC:7765')


def test_gene_identity_leaves_hgnc_id_empty_when_vep_omits_it() -> None:
    annotation = {
        'most_severe_consequence': 'missense_variant',
        'transcript_consequences': [{'gene_symbol': 'NF1', 'consequence_terms': ['missense_variant']}],
    }
    result = vep.parse_vep(annotation, dataset_versions=(f'VEP {_RELEASE}', 'GRCh38'), query='q', dbnsfp_columns=())
    assert result.gene_symbol == 'NF1'
    assert result.hgnc_id == ''  # never fabricated


@pytest.mark.parametrize(
    ('so_term', 'expected'),
    [
        ('missense_variant', evidence_pb2.CONSEQUENCE_MISSENSE),
        ('stop_gained', evidence_pb2.CONSEQUENCE_NONSENSE),
        ('frameshift_variant', evidence_pb2.CONSEQUENCE_FRAMESHIFT),
        ('splice_acceptor_variant', evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE),
        ('splice_donor_variant', evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE),
        ('intron_variant', evidence_pb2.CONSEQUENCE_INTRONIC),
        ('synonymous_variant', evidence_pb2.CONSEQUENCE_SYNONYMOUS),
        ('inframe_insertion', evidence_pb2.CONSEQUENCE_INFRAME_INDEL),
        ('inframe_deletion', evidence_pb2.CONSEQUENCE_INFRAME_INDEL),
        ('start_lost', evidence_pb2.CONSEQUENCE_START_LOST),
        ('stop_lost', evidence_pb2.CONSEQUENCE_STOP_LOST),
        ('mature_miRNA_variant', evidence_pb2.CONSEQUENCE_UNSPECIFIED),
        ('', evidence_pb2.CONSEQUENCE_UNSPECIFIED),
    ],
)
def test_consequence_for_so_term(so_term: str, expected: int) -> None:
    assert vep.consequence_for_so_term(so_term) == expected


@pytest.mark.parametrize('variant', ['17-31232881-G-C', '17:g.41209079dup'])
def test_an_assembly_less_reference_never_reaches_the_endpoint(variant: str) -> None:
    """The adapter holds its own callers to the precondition, so the request is never issued.

    The bare chromosome name is the one that matters: Ensembl would answer it 200, at whichever
    assembly the host serves.
    """

    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f'{variant!r} names no assembly and must be refused before the call')

    async def run() -> vep.VepResult:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await vep.fetch_vep(variant, [], 'GRCh38', http_client=client)

    with pytest.raises(errors.InvalidRequestError, match='names its assembly'):
        asyncio.run(run())


@pytest.mark.parametrize('decorated', ['NM_001042492.3(NF1):c.3496G>C', 'NM_001042492.3(NF1):c.3496G>C (p.Gly1166Arg)'])
def test_clinvars_decorations_are_stripped_before_the_request(decorated: str) -> None:
    """Ensembl annotates the decorated form, but the bare one is what provenance should replay."""
    assert _requested_expression(decorated) == _VARIANT


def test_a_genomic_reference_keeps_its_transcript_qualifier() -> None:
    """`NG_1.1(NM_2.3):c.…` is standard HGVS, not a gene decoration.

    Ensembl refuses this form either way, so what is protected is only that the refusal and the
    provenance `query` name the string the caller actually sent rather than a rewritten one.
    """
    qualified = 'NG_009018.1(NM_001042492.3):c.3496G>C'
    assert _requested_expression(qualified) == qualified


def test_a_variant_ensembl_refuses_is_not_a_fault_to_retry() -> None:
    """Ensembl 400s an expression it cannot parse; reissuing it four times cannot change that."""
    with pytest.raises(errors.InvalidRequestError, match='rejected'):
        _fetch(lambda _: httpx2.Response(400, text='Unable to parse HGVS notation'))


def test_empty_list_raises_value_error() -> None:
    with pytest.raises(ValueError, match='no annotation'):
        _fetch(lambda _: httpx2.Response(200, json=[]))


def test_annotation_without_consequence_raises_value_error() -> None:
    with pytest.raises(ValueError, match='most_severe_consequence'):
        _fetch(lambda _: httpx2.Response(200, json=[{'assembly_name': 'GRCh38'}]))


def _annotated(**consequence: object) -> list[dict[str, object]]:
    """A VEP response carrying one transcript consequence with these fields."""
    return [
        {
            'most_severe_consequence': 'missense_variant',
            'transcript_consequences': [{'transcript_id': 'ENST00000358273', **consequence}],
        }
    ]


def _resolved(**consequence: object) -> dict[str, object]:
    """The one transcript consequence `fetch_vep` returns for that response, BayesDel requested."""
    result = _fetch(lambda _: httpx2.Response(200, json=_annotated(**consequence)), ['BayesDel'])
    consequences = result.raw['transcript_consequences']
    assert isinstance(consequences, list)
    return consequences[0]


def test_an_aligned_dbnsfp_column_arrives_as_the_number_it_is() -> None:
    assert _resolved(bayesdel_noaf_score=0.0531436)['bayesdel_noaf_score'] == 0.0531436


def test_a_dbnsfp_column_whose_entries_agree_resolves_to_that_one_value() -> None:
    """A column's per-transcript values arrive comma-joined, with "." where dbNSFP holds none."""
    assert _resolved(bayesdel_noaf_score='0.510,.,0.510')['bayesdel_noaf_score'] == 0.510


def test_a_dbnsfp_column_naming_several_values_is_an_error_not_a_pick() -> None:
    """Which transcript the score belongs to decides MIS_PRD, so guessing is worse than failing."""
    with pytest.raises(ValueError, match='2 different'):
        _resolved(bayesdel_noaf_score='0.510,0.220')


def test_a_dbnsfp_column_holding_nothing_is_absent_rather_than_a_placeholder() -> None:
    """Left in place, dbNSFP's "." reads as a score."""
    assert 'bayesdel_noaf_score' not in _resolved(bayesdel_noaf_score='.,.')


def test_a_dbnsfp_column_that_is_not_a_number_is_an_error() -> None:
    """Ensembl answers a column its dbNSFP build does not carry with the string "invalid_field"."""
    with pytest.raises(ValueError, match='non-numeric'):
        _resolved(bayesdel_noaf_score='invalid_field')


def test_a_comma_joined_field_the_request_did_not_name_is_left_as_it_is() -> None:
    """`ensembl_transcriptid` is comma-joined too, and is a list rather than one unresolved value."""
    consequence = _resolved(ensembl_transcriptid='ENST00000358273,ENST00000356175', bayesdel_noaf_score=0.1)

    assert consequence['ensembl_transcriptid'] == 'ENST00000358273,ENST00000356175'
