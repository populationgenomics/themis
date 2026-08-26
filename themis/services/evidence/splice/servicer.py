"""The splice gRPC servicer: implements the `Splice` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import splice_pb2, splice_pb2_grpc
from themis.services.evidence import errors, requests, serving
from themis.services.evidence.splice import backend as splice_backend


class Servicer(splice_pb2_grpc.SpliceServicer, serving.EvidenceServicer):
    def __init__(self, backend: splice_backend.SpliceBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def PredictDeltas(
        self, request: splice_pb2.PredictDeltasRequest, context: grpc.aio.ServicerContext
    ) -> splice_pb2.PredictDeltasResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'PredictDeltas', self._predict_deltas(request))

    @override
    async def PredictSkipOutcome(
        self, request: splice_pb2.PredictSkipOutcomeRequest, context: grpc.aio.ServicerContext
    ) -> splice_pb2.PredictSkipOutcomeResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'PredictSkipOutcome', self._predict_skip_outcome(request))

    async def _predict_deltas(self, request: splice_pb2.PredictDeltasRequest) -> splice_pb2.PredictDeltasResponse:
        """PredictDeltas on the accepted request: the Broad services score an id they cannot parse as absent."""
        requests.require_positional_id('PredictDeltas', 'variant', request.variant)
        return await self._backend.predict_deltas(request)

    async def _predict_skip_outcome(
        self, request: splice_pb2.PredictSkipOutcomeRequest
    ) -> splice_pb2.PredictSkipOutcomeResponse:
        requests.require_transcript('PredictSkipOutcome', request.transcript)
        requests.require_genome_build('PredictSkipOutcome', request.genome_build)
        match request.WhichOneof('affected'):
            case None:
                raise errors.InvalidRequestError(
                    'PredictSkipOutcome takes the affected exon: either `exon` or a `cds_position` inside it'
                )
            # Exon numbering is 1-based; the upper bound needs the exon table and is checked there.
            case 'exon' if request.exon < 1:
                raise errors.InvalidRequestError(f'PredictSkipOutcome takes a 1-based exon number; got {request.exon}')
            case _:
                return await self._backend.predict_skip_outcome(request)
