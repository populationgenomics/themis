"""The vep gRPC servicer: implements the `Vep` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import vep_pb2, vep_pb2_grpc
from themis.services.evidence import hgvs, serving
from themis.services.evidence.upstreams import vep
from themis.services.evidence.vep import backend as vep_backend


class Servicer(vep_pb2_grpc.VepServicer, serving.EvidenceServicer):
    def __init__(self, backend: vep_backend.VepBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def Annotate(
        self, request: vep_pb2.AnnotateRequest, context: grpc.aio.ServicerContext
    ) -> vep_pb2.AnnotateResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'Annotate', self._annotate(request))

    async def _annotate(self, request: vep_pb2.AnnotateRequest) -> vep_pb2.AnnotateResponse:
        """Annotate on the accepted request: copied, so a field added to the proto is never dropped here.

        The predictor names are held to the set the adapter knows a wire form for. Ensembl ignores a
        flag it does not recognise, so an unlisted one would answer 200 with the score absent — a
        response no caller can tell from the variant genuinely having none.
        """
        accepted = vep_pb2.AnnotateRequest()
        accepted.CopyFrom(request)
        accepted.variant = hgvs.accepted_hgvs('Annotate', request.variant)
        vep.accepted_predictors('Annotate', request.predictors)
        return await self._backend.annotate(accepted)
