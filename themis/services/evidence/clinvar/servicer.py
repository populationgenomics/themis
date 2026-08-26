"""The clinvar gRPC servicer: implements the `ClinVar` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import clinvar_pb2, clinvar_pb2_grpc
from themis.services.evidence import errors, requests, serving
from themis.services.evidence.clinvar import backend as clinvar_backend
from themis.svcv4 import frequency

# The most records `DescribeVariant` will fetch into one pool: at ~10 s per 500, what fits beside the
# allele lookup inside the rpc deadline. A request above it is refused rather than run to the deadline.
_MAX_POOL_RECORDS = 2000

# And the most `SearchCodingSpan` will fetch into one census. Lower because its other leg is longer:
# the exon table comes from VariantValidator, whose client self-extends to 60 s, so one esummary page
# is what the rpc's remaining budget covers. Accepting more would refuse the request at the deadline
# instead of at the bound, which is what the bound exists to prevent.
_MAX_SPAN_RECORDS = 500


class Servicer(clinvar_pb2_grpc.ClinVarServicer, serving.EvidenceServicer):
    def __init__(self, backend: clinvar_backend.ClinVarBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def DescribeVariant(
        self, request: clinvar_pb2.DescribeVariantRequest, context: grpc.aio.ServicerContext
    ) -> clinvar_pb2.DescribeVariantResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'DescribeVariant', self._describe_variant(request))

    @override
    async def SearchCodingSpan(
        self, request: clinvar_pb2.SearchCodingSpanRequest, context: grpc.aio.ServicerContext
    ) -> clinvar_pb2.SearchCodingSpanResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'SearchCodingSpan', self._search_coding_span(request))

    async def _describe_variant(
        self, request: clinvar_pb2.DescribeVariantRequest
    ) -> clinvar_pb2.DescribeVariantResponse:
        """DescribeVariant on the accepted request: the archive lookup is keyed on the accession.

        An unset `vcv` is a request for the gene pool alone — the crosswalk named no ClinVar
        variation for the allele, which is the ordinary case for a novel one. A set one is held to
        the zero-padded accession efetch resolves. An absent gene would drop the gene clause from
        the pool term, which then answers with ClinVar's whole P/LP set.
        """
        gene = requests.require_gene('DescribeVariant', request.gene, purpose='for the gene pool')
        if request.vcv:
            requests.require_vcv_accession('DescribeVariant', request.vcv)
        if not 0 <= request.review_status_floor <= frequency.MAX_REVIEW_STARS:
            raise errors.InvalidRequestError(
                f'DescribeVariant takes review_status_floor as a ClinVar gold-star count '
                f'0-{frequency.MAX_REVIEW_STARS}; got {request.review_status_floor}'
            )
        if not 1 <= request.max_pool_records <= _MAX_POOL_RECORDS:
            raise errors.InvalidRequestError(
                f'DescribeVariant takes max_pool_records as a bound of 1-{_MAX_POOL_RECORDS} on the gene pool; got '
                f'{request.max_pool_records}. The pool costs ~10s per 500 records, so there is no bound to '
                'default to; to reach further into a well-studied gene, raise review_status_floor instead'
            )
        accepted = clinvar_pb2.DescribeVariantRequest()
        accepted.CopyFrom(request)
        accepted.gene = gene
        return await self._backend.describe_variant(accepted)

    async def _search_coding_span(
        self, request: clinvar_pb2.SearchCodingSpanRequest
    ) -> clinvar_pb2.SearchCodingSpanResponse:
        """SearchCodingSpan on the accepted request: a c. range on one transcript, in transcript order.

        The gene is not a request field — it is read off the transcript's own exon table, so a symbol
        and an accession cannot disagree here the way `Transcript.AssessExonRelevance` has to guard
        against.
        """
        requests.require_transcript('SearchCodingSpan', request.transcript)
        requests.require_cds_range('SearchCodingSpan', request.cds_start, request.cds_end)
        if not 1 <= request.max_records <= _MAX_SPAN_RECORDS:
            raise errors.InvalidRequestError(
                f'SearchCodingSpan takes max_records as a bound of 1-{_MAX_SPAN_RECORDS} on the span census; got '
                f'{request.max_records}. A codon or an exon holds tens of records, so the bound is a guard: '
                'a span that reaches it is wider than the rule being answered.'
            )
        return await self._backend.search_coding_span(request)
