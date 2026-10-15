"""The gene_disease gRPC servicer: implements the `GeneDisease` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.rpc import gene_disease_pb2, gene_disease_pb2_grpc
from themis.services.evidence import requests, serving
from themis.services.evidence.gene_disease import backend as gene_disease_backend


class Servicer(gene_disease_pb2_grpc.GeneDiseaseServicer, serving.EvidenceServicer):
    def __init__(self, backend: gene_disease_backend.GeneDiseaseBackend) -> None:
        self._backend = backend

    @override
    async def DescribeGene(
        self, request: gene_disease_pb2.DescribeGeneRequest, context: grpc.aio.ServicerContext
    ) -> gene_disease_pb2.DescribeGeneResponse:
        return await self._response_or_abort(context, 'DescribeGene', self._describe_gene(request))

    async def _describe_gene(
        self, request: gene_disease_pb2.DescribeGeneRequest
    ) -> gene_disease_pb2.DescribeGeneResponse:
        """DescribeGene on the accepted request: the gene by HGNC id, the disease term by ontology id or not at all.

        A value that is not a MONDO curie at all would resolve against no curation and come back as
        "the gene is curated for other terms than yours" — a statement about the curations rather than
        about the request that produced it. A well-formed curie the ontology does not hold is not
        caught here and answers the same way; the rpc asks MONDO about curated terms, not requested
        ones.
        """
        requests.require_hgnc_id('DescribeGene', request.hgnc_id)
        if request.mondo_id:
            requests.require_mondo_id('DescribeGene', request.mondo_id)
        return await self._backend.describe_gene(request)
