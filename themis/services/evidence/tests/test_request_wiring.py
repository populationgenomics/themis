"""Each shared request precondition, driven over every rpc that takes it, on a real server.

`requests` holds the checks and `test_requests` holds each to its rule; neither says an rpc runs one.
A validator serving a single rpc is covered by that interface's own suite, but one serving several is
covered nowhere unless every rpc that takes it is driven — and these interfaces are exactly where an
unwired check is silent rather than loud: the upstreams answer a field they cannot parse with a
success carrying an absence, which the adapters report as NOT_FOUND and the caller scores as a
finding about a variant nobody asked about.

One rejected value per validator, not the set: which values a check refuses is `test_requests`'s
question, and what is under test here is that the rpc asks at all. Each request is otherwise
acceptable, so the field under test is the only one that can fail it. The rpc's own name is asserted
in the message as well, since every call site passes it in by hand.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import (
    auth_pb2,
    clinvar_pb2,
    clinvar_pb2_grpc,
    cspec_pb2,
    cspec_pb2_grpc,
    gnomad_pb2,
    gnomad_pb2_grpc,
    splice_pb2,
    splice_pb2_grpc,
    transcript_pb2,
    transcript_pb2_grpc,
    variant_pb2,
    variant_pb2_grpc,
)
from themis.services.evidence.clinvar import backend as clinvar_backend
from themis.services.evidence.clinvar import servicer as clinvar_servicer
from themis.services.evidence.cspec import backend as cspec_backend
from themis.services.evidence.cspec import servicer as cspec_servicer
from themis.services.evidence.gnomad import backend as gnomad_backend
from themis.services.evidence.gnomad import servicer as gnomad_servicer
from themis.services.evidence.splice import backend as splice_backend
from themis.services.evidence.splice import servicer as splice_servicer
from themis.services.evidence.transcript import backend as transcript_backend
from themis.services.evidence.transcript import servicer as transcript_servicer
from themis.services.evidence.variant import backend as variant_backend
from themis.services.evidence.variant import servicer as variant_servicer
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)

# The values each rpc's other fields carry, so the one under test is the only one that can fail.
_POSITIONAL_ID = '17-31232881-G-C'
_TRANSCRIPT = 'NM_001042492.3'
_TRANSCRIPT_HGVS = 'NM_001042492.3:c.3496G>C'
_VCV = 'VCV001731988'
_GENOME_BUILD = 'GRCh38'
_GENE = 'NF1'
_EXON = 26
_DATASET = 'gnomad_r4'
_POOL_RECORDS = 500
_SPAN_RECORDS = 50

# And the value each check refuses.
_COLON_SEPARATED_ID = '17:31232881:G:C'  # neither positional upstream parses it
_UNVERSIONED_TRANSCRIPT = 'NM_001042492'  # names an exon structure the caller cannot reproduce
_UNALIGNED_GENOME_BUILD = 'hg38'
_ABSENT_GENE = ' '


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _register_gnomad(server: grpc.aio.Server) -> None:
    gnomad_pb2_grpc.add_GnomadServicer_to_server(
        gnomad_servicer.Servicer(gnomad_backend.FixtureBackend({}), _session_resolver), server
    )


def _register_splice(server: grpc.aio.Server) -> None:
    splice_pb2_grpc.add_SpliceServicer_to_server(
        splice_servicer.Servicer(splice_backend.FixtureBackend({}, {}), _session_resolver), server
    )


def _register_transcript(server: grpc.aio.Server) -> None:
    transcript_pb2_grpc.add_TranscriptServicer_to_server(
        transcript_servicer.Servicer(transcript_backend.FixtureBackend({}, {}), _session_resolver), server
    )


def _register_clinvar(server: grpc.aio.Server) -> None:
    clinvar_pb2_grpc.add_ClinVarServicer_to_server(
        clinvar_servicer.Servicer(clinvar_backend.FixtureBackend({}, {}), _session_resolver), server
    )


def _register_variant(server: grpc.aio.Server) -> None:
    variant_pb2_grpc.add_VariantServicer_to_server(
        variant_servicer.Servicer(variant_backend.FixtureBackend({}), _session_resolver), server
    )


def _register_cspec(server: grpc.aio.Server) -> None:
    cspec_pb2_grpc.add_CspecServicer_to_server(
        cspec_servicer.Servicer(cspec_backend.FixtureBackend({}), _session_resolver), server
    )


async def _gnomad_describe_variant(*, gnomad_id: str = _POSITIONAL_ID, cooccurrence_with: str = '') -> None:
    async with in_process_grpc.serving(_register_gnomad) as channel:
        await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(
            gnomad_pb2.DescribeVariantRequest(
                gnomad_id=gnomad_id, dataset=_DATASET, cooccurrence_with=cooccurrence_with
            ),
            metadata=_GOOD_TOKEN,
        )


async def _splice_predict_deltas(*, variant: str = _POSITIONAL_ID) -> None:
    async with in_process_grpc.serving(_register_splice) as channel:
        await splice_pb2_grpc.SpliceStub(channel).PredictDeltas(
            splice_pb2.PredictDeltasRequest(variant=variant), metadata=_GOOD_TOKEN
        )


async def _splice_predict_skip_outcome(*, transcript: str = _TRANSCRIPT, genome_build: str = _GENOME_BUILD) -> None:
    async with in_process_grpc.serving(_register_splice) as channel:
        await splice_pb2_grpc.SpliceStub(channel).PredictSkipOutcome(
            splice_pb2.PredictSkipOutcomeRequest(transcript=transcript, genome_build=genome_build, exon=_EXON),
            metadata=_GOOD_TOKEN,
        )


async def _transcript_get_structure(*, transcript: str = _TRANSCRIPT, genome_build: str = _GENOME_BUILD) -> None:
    async with in_process_grpc.serving(_register_transcript) as channel:
        await transcript_pb2_grpc.TranscriptStub(channel).GetStructure(
            transcript_pb2.GetStructureRequest(transcript=transcript, genome_build=genome_build),
            metadata=_GOOD_TOKEN,
        )


async def _transcript_assess_exon_relevance(*, transcript: str = _TRANSCRIPT, gene: str = _GENE) -> None:
    async with in_process_grpc.serving(_register_transcript) as channel:
        await transcript_pb2_grpc.TranscriptStub(channel).AssessExonRelevance(
            transcript_pb2.AssessExonRelevanceRequest(gene=gene, transcript=transcript, exon=_EXON),
            metadata=_GOOD_TOKEN,
        )


async def _clinvar_describe_variant(*, gene: str = _GENE) -> None:
    async with in_process_grpc.serving(_register_clinvar) as channel:
        await clinvar_pb2_grpc.ClinVarStub(channel).DescribeVariant(
            clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene=gene, max_pool_records=_POOL_RECORDS),
            metadata=_GOOD_TOKEN,
        )


async def _clinvar_search_coding_span(*, transcript: str = _TRANSCRIPT) -> None:
    async with in_process_grpc.serving(_register_clinvar) as channel:
        await clinvar_pb2_grpc.ClinVarStub(channel).SearchCodingSpan(
            clinvar_pb2.SearchCodingSpanRequest(
                transcript=transcript, cds_start=1108, cds_end=1110, max_records=_SPAN_RECORDS
            ),
            metadata=_GOOD_TOKEN,
        )


async def _variant_normalize(*, genome_build: str = _GENOME_BUILD) -> None:
    async with in_process_grpc.serving(_register_variant) as channel:
        await variant_pb2_grpc.VariantStub(channel).Normalize(
            variant_pb2.NormalizeRequest(variant=_TRANSCRIPT_HGVS, genome_build=genome_build), metadata=_GOOD_TOKEN
        )


async def _cspec_list_specifications(*, gene: str = _GENE) -> None:
    async with in_process_grpc.serving(_register_cspec) as channel:
        await cspec_pb2_grpc.CspecStub(channel).ListSpecifications(
            cspec_pb2.ListSpecificationsRequest(gene=gene), metadata=_GOOD_TOKEN
        )


def _refused(call: Callable[[], Awaitable[None]]) -> str:
    """Drive one rpc whose request a precondition must refuse, and hand back the rejection's detail.

    An unwired check does not surface as a raise here: the request reaches the fixture backend, which
    answers an unseeded key with NOT_FOUND. The status is what tells the two apart.
    """

    async def run() -> grpc.aio.AioRpcError:
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await call()
        return caught.value

    failure = asyncio.run(run())
    assert failure.code() == grpc.StatusCode.INVALID_ARGUMENT
    return failure.details() or ''


@pytest.mark.parametrize(
    ('rpc', 'field', 'call'),
    [
        ('DescribeVariant', 'gnomad_id', lambda: _gnomad_describe_variant(gnomad_id=_COLON_SEPARATED_ID)),
        (
            'DescribeVariant',
            'cooccurrence_with',
            lambda: _gnomad_describe_variant(cooccurrence_with=_COLON_SEPARATED_ID),
        ),
        ('PredictDeltas', 'variant', lambda: _splice_predict_deltas(variant=_COLON_SEPARATED_ID)),
    ],
    ids=['gnomad.gnomad_id', 'gnomad.cooccurrence_with', 'splice.variant'],
)
def test_every_positional_field_is_refused_where_its_upstream_would_score_it_as_absent(
    rpc: str, field: str, call: Callable[[], Awaitable[None]]
) -> None:
    """Both upstreams answer an id they cannot parse with an absence, not with an error.

    For these rpcs the absence IS the evidence — rarity, and an unscorable position — so an unwired
    check turns a caller's typo into a POP_FRQ or SPL_PRD finding, and nothing retries it.
    """
    details = _refused(call)
    assert 'chrom-pos-ref-alt' in details
    assert rpc in details
    assert field in details


@pytest.mark.parametrize(
    ('rpc', 'call'),
    [
        ('GetStructure', lambda: _transcript_get_structure(transcript=_UNVERSIONED_TRANSCRIPT)),
        ('AssessExonRelevance', lambda: _transcript_assess_exon_relevance(transcript=_UNVERSIONED_TRANSCRIPT)),
        ('PredictSkipOutcome', lambda: _splice_predict_skip_outcome(transcript=_UNVERSIONED_TRANSCRIPT)),
        ('SearchCodingSpan', lambda: _clinvar_search_coding_span(transcript=_UNVERSIONED_TRANSCRIPT)),
    ],
    ids=['transcript.structure', 'transcript.exon_relevance', 'splice.outcome', 'clinvar.span'],
)
def test_every_rpc_keyed_on_an_exon_table_refuses_a_transcript_it_cannot_look_one_up_by(
    rpc: str, call: Callable[[], Awaitable[None]]
) -> None:
    details = _refused(call)
    assert 'RefSeq coding transcript accession' in details
    assert rpc in details


@pytest.mark.parametrize(
    ('rpc', 'call'),
    [
        ('Normalize', lambda: _variant_normalize(genome_build=_UNALIGNED_GENOME_BUILD)),
        ('GetStructure', lambda: _transcript_get_structure(genome_build=_UNALIGNED_GENOME_BUILD)),
        ('PredictSkipOutcome', lambda: _splice_predict_skip_outcome(genome_build=_UNALIGNED_GENOME_BUILD)),
    ],
    ids=['variant.normalize', 'transcript.structure', 'splice.outcome'],
)
def test_every_rpc_taking_a_genome_build_refuses_one_the_upstreams_do_not_align_to(
    rpc: str, call: Callable[[], Awaitable[None]]
) -> None:
    details = _refused(call)
    assert 'genome_build' in details
    assert rpc in details


@pytest.mark.parametrize(
    ('rpc', 'call'),
    [
        ('DescribeVariant', lambda: _clinvar_describe_variant(gene=_ABSENT_GENE)),
        ('AssessExonRelevance', lambda: _transcript_assess_exon_relevance(gene=_ABSENT_GENE)),
        ('ListSpecifications', lambda: _cspec_list_specifications(gene=_ABSENT_GENE)),
    ],
    ids=['clinvar.describe_variant', 'transcript.exon_relevance', 'cspec.list_specifications'],
)
def test_every_gene_scoped_rpc_refuses_a_request_that_names_no_gene(
    rpc: str, call: Callable[[], Awaitable[None]]
) -> None:
    """Each of these scopes its query by the symbol, and an absent one widens the query silently."""
    details = _refused(call)
    assert 'HGNC symbol' in details
    assert rpc in details
