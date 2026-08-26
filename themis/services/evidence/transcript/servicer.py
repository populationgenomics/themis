"""The transcript gRPC servicer: implements the `Transcript` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import transcript_pb2, transcript_pb2_grpc
from themis.services.evidence import errors, requests, serving
from themis.services.evidence.transcript import backend as transcript_backend


class Servicer(transcript_pb2_grpc.TranscriptServicer, serving.EvidenceServicer):
    def __init__(
        self, backend: transcript_backend.TranscriptBackend, session_resolver: session_mod.SessionResolver
    ) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def GetStructure(
        self, request: transcript_pb2.GetStructureRequest, context: grpc.aio.ServicerContext
    ) -> transcript_pb2.GetStructureResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'GetStructure', self._get_structure(request))

    @override
    async def AssessExonRelevance(
        self, request: transcript_pb2.AssessExonRelevanceRequest, context: grpc.aio.ServicerContext
    ) -> transcript_pb2.AssessExonRelevanceResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'AssessExonRelevance', self._assess_exon_relevance(request))

    async def _get_structure(self, request: transcript_pb2.GetStructureRequest) -> transcript_pb2.GetStructureResponse:
        requests.require_transcript('GetStructure', request.transcript)
        requests.require_genome_build('GetStructure', request.genome_build)
        return await self._backend.get_structure(request)

    async def _assess_exon_relevance(
        self, request: transcript_pb2.AssessExonRelevanceRequest
    ) -> transcript_pb2.AssessExonRelevanceResponse:
        """AssessExonRelevance on the accepted request: pext is read off each exon's genomic span.

        The gene names the transcript set the inventory is a denominator over, as well as scoping
        four upstream queries. The MANE flags are echoed back, so an unsent one would arrive as the
        false/false that forces "Few" — presence is what separates the two.
        """
        gene = requests.require_gene(
            'AssessExonRelevance', request.gene, purpose='for the gene-scoped signals and the transcript inventory'
        )
        requests.require_transcript('AssessExonRelevance', request.transcript)
        if request.exon < 1:
            raise errors.InvalidRequestError(f'AssessExonRelevance takes a 1-based exon number; got {request.exon}')
        for flag in ('in_mane_select', 'in_mane_plus_clinical'):
            if not request.HasField(flag):
                raise errors.InvalidRequestError(
                    f"AssessExonRelevance takes {flag} from the allele's TranscriptProjection "
                    '(Variant.Normalize); membership in neither forces "Few", so an unset flag cannot '
                    'stand for a false one'
                )
        accepted = transcript_pb2.AssessExonRelevanceRequest()
        accepted.CopyFrom(request)
        accepted.gene = gene
        return await self._backend.assess_exon_relevance(accepted)
