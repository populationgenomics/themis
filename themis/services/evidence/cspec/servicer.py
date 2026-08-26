"""The cspec gRPC servicer: implements the `Cspec` service from the proto contract."""

from __future__ import annotations

from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import cspec_pb2, cspec_pb2_grpc
from themis.services.evidence import requests, serving
from themis.services.evidence.cspec import backend as cspec_backend


class Servicer(cspec_pb2_grpc.CspecServicer, serving.EvidenceServicer):
    def __init__(self, backend: cspec_backend.CspecBackend, session_resolver: session_mod.SessionResolver) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def ListSpecifications(
        self, request: cspec_pb2.ListSpecificationsRequest, context: grpc.aio.ServicerContext
    ) -> cspec_pb2.ListSpecificationsResponse:
        await self._require_session(context)
        return await self._response_or_abort(context, 'ListSpecifications', self._list_specifications(request))

    async def _list_specifications(
        self, request: cspec_pb2.ListSpecificationsRequest
    ) -> cspec_pb2.ListSpecificationsResponse:
        """ListSpecifications on the accepted request: the registry's gene table is the only key.

        The symbol is stripped and nothing else: the registry keys on HGNC's approved symbol
        case-sensitively, and case-folding one here would turn a symbol it has no entry for into a
        lookup that answers about no gene. That miss is `SPECIFICATION_COVERAGE_GENE_ABSENT`, which
        the caller reads apart from "no panel has specified this gene".
        """
        accepted = cspec_pb2.ListSpecificationsRequest()
        accepted.CopyFrom(request)
        accepted.gene = requests.require_gene(
            'ListSpecifications', request.gene, purpose='naming the gene whose specifications to read'
        )
        return await self._backend.list_specifications(accepted)
