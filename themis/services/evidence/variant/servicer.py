"""The variant gRPC servicer: implements the `Variant` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import variant_pb2, variant_pb2_grpc
from themis.services.evidence import hgvs, requests, serving
from themis.services.evidence.variant import backend as variant_backend


class Servicer(variant_pb2_grpc.VariantServicer, serving.EvidenceServicer):
    def __init__(self, backend: variant_backend.VariantBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def Normalize(
        self, request: variant_pb2.NormalizeRequest, context: grpc.aio.ServicerContext
    ) -> variant_pb2.NormalizeResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'Normalize', self._normalize(request))

    async def _normalize(self, request: variant_pb2.NormalizeRequest) -> variant_pb2.NormalizeResponse:
        """Normalize on the accepted request: copied, so a field added to the proto is never dropped here."""
        accepted = variant_pb2.NormalizeRequest()
        accepted.CopyFrom(request)
        accepted.variant = hgvs.accepted_transcript_hgvs('Normalize', request.variant)
        requests.require_genome_build('Normalize', request.genome_build)
        return await self._backend.normalize(accepted)
