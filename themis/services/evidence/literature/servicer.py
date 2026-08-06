"""The literature gRPC servicer: implements the ``Literature`` service from the proto contract.

Subclasses the generated ``themis.rpc.literature_pb2_grpc.LiteratureServicer`` (the forced interface),
delegating to the injected backend and mapping its typed failures to transport status codes:
an unknown doc_id is NOT_FOUND, a missing selected object is NOT_FOUND, a requested representation
the paper lacks is FAILED_PRECONDITION, and an unset selector / unspecified representation is
INVALID_ARGUMENT. A quote that does not locate is a modelled ``not_located`` result, not an error.
"""

from __future__ import annotations

import grpc

from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend


class Servicer(literature_pb2_grpc.LiteratureServicer):
    def __init__(self, backend: literature_backend.LiteratureBackend) -> None:
        self._backend = backend

    async def DescribePaper(
        self, request: literature_pb2.DescribePaperRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.PaperInfo:
        try:
            return await self._backend.describe_paper(request.doc_id)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f'unknown doc_id {request.doc_id!r}')

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

    async def Validate(
        self, request: literature_pb2.ValidateRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ValidateResponse:
        del context  # required by the servicer interface; Validate never aborts
        return await self._backend.validate(request.doc_id, request.quote)


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
