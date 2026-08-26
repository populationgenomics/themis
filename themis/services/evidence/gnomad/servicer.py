"""The gnomad gRPC servicer: implements the `Gnomad` service from the proto contract.

The ids are held to the shared positional-id precondition (`requests`); the dataset is this rpc's
own, so `_require_dataset` lives here.
"""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import gnomad_pb2, gnomad_pb2_grpc
from themis.services.evidence import errors, requests, serving
from themis.services.evidence.gnomad import backend as gnomad_backend

# The datasets this rpc serves. Narrower than the browser's `DatasetId` enum, which also resolves
# `gnomad_r3` and others: v4 is the frequency source and v2 the only one carrying co-occurrence, so
# a third release would be a silent change of denominator rather than a new fact.
_DATASETS = ('gnomad_r4', 'gnomad_r2_1')


class Servicer(gnomad_pb2_grpc.GnomadServicer, serving.EvidenceServicer):
    def __init__(self, backend: gnomad_backend.GnomadBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def DescribeVariant(
        self, request: gnomad_pb2.DescribeVariantRequest, context: grpc.aio.ServicerContext
    ) -> gnomad_pb2.DescribeVariantResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'DescribeVariant', self._describe_variant(request))

    async def _describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        """DescribeVariant on the accepted request: both ids and the dataset, none of which the wire checks."""
        requests.require_positional_id('DescribeVariant', 'gnomad_id', request.gnomad_id)
        if request.cooccurrence_with:
            requests.require_positional_id('DescribeVariant', 'cooccurrence_with', request.cooccurrence_with)
        self._require_dataset(request.dataset)
        return await self._backend.describe_variant(request)

    @staticmethod
    def _require_dataset(dataset: str) -> None:
        """Hold the dataset to the two releases the rpc's answers are defined against.

        Policy, not a limit of the upstream: gnomAD resolves `gnomad_r3` too, and answering from it
        would change the allele-frequency denominator under a caller reading the result as a v4 FAF.
        A value outside the enum is a 500 there, so an unchecked one would also arrive as an
        uncharacterised fault and be retried four times.

        Raises:
            errors.InvalidRequestError: If `dataset` is not one of the two the rpc serves.
        """
        if dataset not in _DATASETS:
            raise errors.InvalidRequestError(f'DescribeVariant takes dataset {" or ".join(_DATASETS)}; got {dataset!r}')
