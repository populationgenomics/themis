"""The gene_disease gRPC servicer: implements the `GeneDisease` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2, gene_disease_pb2_grpc
from themis.services.evidence import errors, requests, serving
from themis.services.evidence.gene_disease import backend as gene_disease_backend


class Servicer(gene_disease_pb2_grpc.GeneDiseaseServicer, serving.EvidenceServicer):
    def __init__(
        self, backend: gene_disease_backend.GeneDiseaseBackend, session_resolver: session_mod.SessionResolver
    ) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def DescribeGene(
        self, request: gene_disease_pb2.DescribeGeneRequest, context: grpc.aio.ServicerContext
    ) -> gene_disease_pb2.DescribeGeneResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'DescribeGene', self._describe_gene(request))

    async def _describe_gene(
        self, request: gene_disease_pb2.DescribeGeneRequest
    ) -> gene_disease_pb2.DescribeGeneResponse:
        """DescribeGene on the accepted request: the gene by HGNC id, the entity by ontology id or not at all.

        A value that is not a MONDO curie at all, and an inheritance outside the enum (proto3 carries
        an unknown enum number through), would each resolve against no curation and come back as "the
        gene is curated for other entities than yours" — a statement about the curations rather than
        about the request that produced it. A well-formed curie the ontology does not hold is not
        caught here and answers the same way; the rpc asks MONDO about curated terms, not requested
        ones.
        """
        requests.require_hgnc_id('DescribeGene', request.hgnc_id)
        if request.mondo_id:
            requests.require_mondo_id('DescribeGene', request.mondo_id)
        if request.inheritance not in evidence_pb2.Inheritance.values():
            raise errors.InvalidRequestError(
                f'DescribeGene takes inheritance from {evidence_pb2.Inheritance.keys()}; got {request.inheritance!r}'
            )
        if request.inheritance != evidence_pb2.INHERITANCE_UNSPECIFIED and not request.mondo_id:
            raise errors.InvalidRequestError(
                'DescribeGene takes inheritance as half of the entity mondo_id names; got an inheritance with no '
                'mondo_id, which narrows nothing'
            )
        return await self._backend.describe_gene(request)
