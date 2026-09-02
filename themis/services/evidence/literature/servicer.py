"""The literature gRPC servicer: implements the ``Literature`` service from the proto contract.

Subclasses the generated ``themis.rpc.literature_pb2_grpc.LiteratureServicer`` (the forced interface),
delegating to the injected backend and mapping its typed failures to transport status codes:
an unknown doc_id is NOT_FOUND, a missing selected object is NOT_FOUND, a requested representation
the paper lacks is FAILED_PRECONDITION, an unset selector / unspecified representation is
INVALID_ARGUMENT, a rendering the manifest lists but the store cannot produce is INTERNAL (the store
broke its own invariant), and a quote against a PDF is UNIMPLEMENTED while no producer resolves one.
A quote that does not locate is a modelled ``not_located`` result, not an error.

``MaybeIngestPapers`` is the one rpc that starts work rather than only reading: the papers it resolves
to PENDING get a conversion requested through the backend, so its failures split three ways — retry
(UNAVAILABLE), fix the deployment (FAILED_PRECONDITION), or page someone (INTERNAL). It is also the
only rpc here that authorizes, and only over its enqueue step — see ``_request_conversions``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence import serving
from themis.services.evidence.literature import backend as literature_backend

_logger = logging.getLogger(__name__)

# A readiness batch costs one to two GCS reads per distinct id — two while unsettled, since the sidecar
# probe only short-circuits on a rendering — on the shared default thread executor every other RPC on
# the instance draws from. Server-side because the caller is not the party that bears the cost.
_MAX_DOC_IDS = 100
# The same bound on the external-id path: each id costs a crosswalk row and then a readiness read.
_MAX_EXTERNAL_IDS = 100
# Crosswalk keys are `{scheme}:{value}`. An unqualified id is rejected rather than guessed at: a bare
# "10.1/x" could be a DOI, and a bare number a PMID, but guessing wrong resolves to another paper.
_ID_SCHEMES = frozenset({'doi', 'pmid', 'pmcid'})


class Servicer(literature_pb2_grpc.LiteratureServicer, serving.EvidenceServicer):
    def __init__(
        self, backend: literature_backend.LiteratureBackend, session_resolver: session_mod.SessionResolver
    ) -> None:
        super().__init__(session_resolver)
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
        except literature_backend.MissingRenderingBlobError as e:
            await context.abort(grpc.StatusCode.INTERNAL, str(e))

    @override
    async def Validate(
        self, request: literature_pb2.ValidateRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ValidateResponse:
        try:
            return await self._backend.validate(request.doc_id, request.quote)
        except literature_backend.MissingRenderingBlobError as e:
            await context.abort(grpc.StatusCode.INTERNAL, str(e))

    @override
    async def PollFullTexts(
        self, request: literature_pb2.PollFullTextsRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.PollFullTextsResponse:
        # An oversized batch aborts; an unknown doc_id does not — it is a per-id UNKNOWN_PAPER state.
        if len(request.doc_ids) > _MAX_DOC_IDS:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f'at most {_MAX_DOC_IDS} doc_ids per request')
        states = await self._backend.full_text_readiness(list(request.doc_ids))
        return literature_pb2.PollFullTextsResponse(
            readiness=[literature_pb2.FullTextReadiness(doc_id=doc_id, state=state) for doc_id, state in states.items()]
        )

    @override
    async def MaybeIngestPapers(
        self, request: literature_pb2.MaybeIngestPapersRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.MaybeIngestPapersResponse:
        external_ids = list(dict.fromkeys(request.external_ids))
        if len(external_ids) > _MAX_EXTERNAL_IDS:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, f'at most {_MAX_EXTERNAL_IDS} external_ids per request'
            )
        malformed = [i for i in external_ids if not _is_scheme_qualified(i)]
        if malformed:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'external_ids must be scheme-qualified ({"/".join(sorted(_ID_SCHEMES))}): {malformed}',
            )
        try:
            doc_ids = await self._backend.resolve_external_ids(external_ids)
        except literature_backend.CrosswalkNotConfiguredError as e:
            # Permanent for this deployment, so not UNAVAILABLE — gRPC retries that by default, and no
            # number of retries wires a crosswalk.
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, f'id resolution is not configured: {e}')
        except literature_backend.CrosswalkUnavailableError:
            # Whole-call, never a per-id empty doc_id: an outage affects the batch, and a caller
            # reading it per-id would write every one of these papers off as absent from the corpus.
            # The driver's detail carries the failing query and the instance connection name, so it
            # goes to the log, not to a caller that reaches the browser and the sandbox agent.
            _logger.exception('crosswalk lookup failed')
            await context.abort(grpc.StatusCode.UNAVAILABLE, 'crosswalk unavailable')
        # Two external ids can name one paper (a DOI and its PMID), so collapse after resolution —
        # where the reads are — and report both ids against the doc_id they share.
        states = await self._backend.full_text_readiness(list(dict.fromkeys(doc_ids.values())))
        await self._request_conversions(states, context)
        return literature_pb2.MaybeIngestPapersResponse(
            readiness=[
                literature_pb2.PaperReadiness(
                    external_id=external_id,
                    doc_id=doc_ids.get(external_id, ''),
                    state=states.get(doc_ids.get(external_id, ''), literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER),
                )
                for external_id in external_ids
            ]
        )

    async def _request_conversions(
        self, states: Mapping[str, literature_pb2.FullTextState], context: grpc.aio.ServicerContext
    ) -> None:
        """Start a conversion for every PENDING paper, aborting the call if one could not be asked for.

        Resolving a session is this step's, not the rpc's: the enqueue is what costs money, the reads
        around it are not. So a batch with nothing to produce is answered without a token, and one with
        something to produce is refused whole (``evidence-fulltext.md``).

        The whole call fails rather than answering readiness anyway: an enqueue that did not happen
        leaves the paper PENDING, which is indistinguishable from one whose conversion is under way, so
        a caller told "PENDING" would have no reason to ask again. Repeating the call is the remedy, and
        the ``doc_id``-keyed task name makes it free for the papers already queued.
        """
        # READY and the two terminal states need nothing; UNKNOWN_PAPER has no manifest, so there is
        # nothing for the producer to read and no task to name.
        pending = [doc_id for doc_id, state in states.items() if state == literature_pb2.FULL_TEXT_STATE_PENDING]
        if not pending:
            return
        await self._require_session(context)
        try:
            await self._backend.request_conversions(pending)
        except literature_backend.ConversionNotConfiguredError as e:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, f'conversion is not configured: {e}')
        except literature_backend.ConversionUnavailableError:
            # The detail names the queue and the runtime identity, so it goes to the log rather than to
            # a caller that reaches the browser and the sandbox agent.
            _logger.exception('conversion enqueue failed transiently')
            await context.abort(grpc.StatusCode.UNAVAILABLE, 'the conversion queue is unavailable')
        except literature_backend.ConversionEnqueueFailedError:
            _logger.exception('conversion enqueue was refused')
            await context.abort(grpc.StatusCode.INTERNAL, 'the conversion could not be enqueued')


def _is_scheme_qualified(external_id: str) -> bool:
    """Whether `external_id` is `{known scheme}:{non-empty value}`.

    Both halves are checked: `'doi'` and `'doi:'` would otherwise reach the crosswalk as literal keys,
    miss, and come back as an empty doc_id with UNKNOWN_PAPER — reporting a malformed request as the
    settled fact that the corpus does not hold the paper.
    """
    scheme, _, value = external_id.partition(':')
    return scheme in _ID_SCHEMES and bool(value)


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
