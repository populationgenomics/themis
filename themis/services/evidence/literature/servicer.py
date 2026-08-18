"""The literature gRPC servicer: implements the ``Literature`` service from the proto contract.

Subclasses the generated ``themis.rpc.literature_pb2_grpc.LiteratureServicer`` (the forced interface),
delegating to the injected backend and mapping its typed failures to transport status codes:
an unknown doc_id is NOT_FOUND, a missing selected object is NOT_FOUND, a requested representation
the paper lacks is FAILED_PRECONDITION, and an unset selector / unspecified representation is
INVALID_ARGUMENT. A quote that does not locate is a modelled ``not_located`` result, not an error.
"""

from __future__ import annotations

from typing import override

import grpc

from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend

# A readiness batch costs one to two GCS reads per distinct id — two while PENDING, since the sidecar
# probe only short-circuits on a rendering — and a wait re-reads every still-PENDING id each poll
# cycle, so one request runs up to `2 * ids * timeout/interval` of them on the shared default thread
# executor that every other RPC on the instance draws from. The batch cap bounds both how many run at
# once and how many run in total; the wait bounds how long they keep coming. Both are server-side
# because the caller is not the party that bears the cost.
_MAX_DOC_IDS = 100
# Cloud Run's default request timeout; the evidence service declares none of its own.
_REQUEST_TIMEOUT_SECONDS = 300.0
# Derived, not chosen: a wait allowed to run the whole request window would be killed by the platform
# while returning, so the caller would get DEADLINE_EXCEEDED instead of the PENDING readiness a long
# wait promises. The margin is the response's budget.
_MAX_AWAIT_SECONDS = _REQUEST_TIMEOUT_SECONDS - 60.0


class Servicer(literature_pb2_grpc.LiteratureServicer):
    def __init__(self, backend: literature_backend.LiteratureBackend) -> None:
        self._backend = backend

    @override
    async def DescribePaper(
        self, request: literature_pb2.DescribePaperRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.PaperInfo:
        try:
            return await self._backend.describe_paper(request.doc_id)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f'unknown doc_id {request.doc_id!r}')

    @override
    async def ResolveContent(
        self, request: literature_pb2.ResolveContentRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ContentLocation:
        selector = _decode_selector(request)
        if selector is None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'ResolveContent requires a selector')
        try:
            return await self._backend.resolve_content(request.doc_id, selector)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f'unknown doc_id {request.doc_id!r}')
        except literature_backend.MissingContentError as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))

    @override
    async def Locate(
        self, request: literature_pb2.LocateRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.LocateResponse:
        if request.representation not in (literature_pb2.REPRESENTATION_MARKDOWN, literature_pb2.REPRESENTATION_PDF):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'Locate requires a known representation')
        try:
            return await self._backend.locate(request.doc_id, request.quote, request.representation)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f'unknown doc_id {request.doc_id!r}')
        except literature_backend.RepresentationUnavailableError as e:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
        except literature_backend.PdfLocationUnavailableError as e:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED, str(e))
        except literature_backend.MissingContentError as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))

    @override
    async def Validate(
        self, request: literature_pb2.ValidateRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ValidateResponse:
        del context  # required by the servicer interface; Validate never aborts
        return await self._backend.validate(request.doc_id, request.quote)

    @override
    async def EnsureFullText(
        self, request: literature_pb2.EnsureFullTextRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.EnsureFullTextResponse:
        # An oversized batch aborts; an unknown doc_id does not — it is a per-id UNKNOWN_PAPER state.
        if len(request.doc_ids) > _MAX_DOC_IDS:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f'at most {_MAX_DOC_IDS} doc_ids per request')
        states = await self._backend.full_text_readiness(list(request.doc_ids))
        return literature_pb2.EnsureFullTextResponse(
            readiness=[literature_pb2.FullTextReadiness(doc_id=doc_id, state=state) for doc_id, state in states.items()]
        )

    @override
    async def AwaitFullText(
        self, request: literature_pb2.AwaitFullTextRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.AwaitFullTextResponse:
        if not request.HasField('timeout'):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'AwaitFullText requires a timeout')
        # Summed from the fields rather than via ToTimedelta: Duration.seconds is an unvalidated
        # int64 on the wire, and timedelta raises OverflowError past ~8.6e13 s — before any guard here.
        timeout_seconds = request.timeout.seconds + request.timeout.nanos / 1e9
        if timeout_seconds < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'AwaitFullText timeout must be non-negative')
        if len(request.doc_ids) > _MAX_DOC_IDS:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f'at most {_MAX_DOC_IDS} doc_ids per request')
        states = await self._backend.await_full_text_readiness(
            list(request.doc_ids), min(timeout_seconds, _MAX_AWAIT_SECONDS)
        )
        return literature_pb2.AwaitFullTextResponse(
            readiness=[literature_pb2.FullTextReadiness(doc_id=doc_id, state=state) for doc_id, state in states.items()]
        )


def _decode_selector(
    request: literature_pb2.ResolveContentRequest,
) -> literature_backend.ContentSelector | None:
    """Decode the ResolveContentRequest.selector oneof, or None when unset."""
    which = request.WhichOneof('selector')
    if which == 'markdown':
        return literature_backend.MarkdownContent()
    if which == 'pdf':
        return literature_backend.PdfContent()
    if which == 'file':
        return literature_backend.FileContent(name=request.file.name)
    return None
