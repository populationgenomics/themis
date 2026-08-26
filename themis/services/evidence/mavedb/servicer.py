"""The mavedb gRPC servicer: implements the `MaveDb` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import mavedb_pb2, mavedb_pb2_grpc
from themis.services.evidence import errors, hgvs, serving
from themis.services.evidence.mavedb import backend as mavedb_backend


class Servicer(mavedb_pb2_grpc.MaveDbServicer, serving.EvidenceServicer):
    def __init__(self, backend: mavedb_backend.MaveDbBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def DescribeVariant(
        self, request: mavedb_pb2.DescribeVariantRequest, context: grpc.aio.ServicerContext
    ) -> mavedb_pb2.DescribeVariantResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'DescribeVariant', self._describe_variant(request))

    async def _describe_variant(self, request: mavedb_pb2.DescribeVariantRequest) -> mavedb_pb2.DescribeVariantResponse:
        """DescribeVariant on the accepted request: copied, so a field added to the proto is never dropped."""
        accepted = mavedb_pb2.DescribeVariantRequest()
        accepted.CopyFrom(request)
        accepted.variant = _accepted_variant(request.variant)
        return await self._backend.describe_variant(accepted)


def _accepted_variant(variant: str) -> str:
    """The variant, in the form the ClinGen Allele Registry registers an allele id for.

    MaveDB is keyed on ClinGen allele ids, so the request has to name an expression the registry
    resolves, and which one is asked decides which ids come back: a coding change registers the
    canonical allele *and* the protein alleles of its transcripts, a protein change only the latter.
    A caller holding either can ask; a bare change with no accession can ask nothing, and would
    otherwise reach MaveDB as a question about no allele and read back as "no assay covers this
    variant" — a scored SVCv4 input.

    Raises:
        errors.InvalidRequestError: If `variant` is neither form.
    """
    if ':p.' in variant:
        return hgvs.accepted_protein_hgvs('DescribeVariant', variant)
    if ':c.' in variant:
        return hgvs.accepted_transcript_hgvs('DescribeVariant', variant)
    raise errors.InvalidRequestError(
        f'DescribeVariant takes the coding or protein HGVS the variant registers a ClinGen allele under, e.g. '
        f'NM_001042492.3:c.3496G>C or NP_001035957.1:p.Gly1166Arg; got {variant!r}'
    )
