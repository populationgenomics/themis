"""The literature gRPC servicer: implements the ``Literature`` service from the proto contract.

Subclasses the generated ``themis.rpc.literature_pb2_grpc.LiteratureServicer`` (the forced interface),
delegating to the injected backend and mapping its typed failures to transport status codes: an
unknown doc_id is NOT_FOUND, a missing selected object is NOT_FOUND, a requested representation the
paper lacks is FAILED_PRECONDITION, a crosswalk this deployment does not wire is FAILED_PRECONDITION
and one that cannot be reached is UNAVAILABLE, and an unset selector / unspecified representation /
an unqualified, malformed or oversized batch / a variant request nothing resolves from is
INVALID_ARGUMENT — and so is a discovery upstream's own refusal of the request as issued
(``errors.InvalidRequestError``), which as UNKNOWN would have the guest's retry helper reissue a
request that cannot come back different. A rendering the manifest lists but the store cannot produce
is INTERNAL — the store broke its own invariant — and a quote against a PDF is UNIMPLEMENTED while no
producer resolves one. A quote that does not locate is a modelled ``not_located`` result, not an
error, and so is a paper whose text the store cannot serve, an id the store holds no paper for, and
an entity set the index holds nothing for.

``MaybeIngestPapers`` is the one rpc that starts work rather than only reading: the papers it resolves
to PENDING get a conversion requested through the backend, so its failures split three ways — retry
(UNAVAILABLE), fix the deployment (FAILED_PRECONDITION), or page someone (INTERNAL). It is also the
only rpc here that authorizes, and only over its enqueue step — see ``_request_conversions``.

The discovery rpcs also own the ceilings: a request's ``max_results`` is clamped here, and each
response carries the source's own total, so a truncated answer is legible as one. Every rpc answers within
the image's per-rpc deadline (``serving.within_deadline``), the database-backed interfaces' bound and this one's
alike: a discovery fan-out that outruns the caller's budget and a store read hung on GCS or Cloud
SQL both end as this service's own DEADLINE_EXCEEDED naming the rpc.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping, Sequence
from typing import override

import grpc

from themis.clients.auth import session as session_mod
from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence import errors, serving
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import external_ids as external_ids_mod
from themis.services.evidence.literature import pmids as pmids_mod
from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc

_logger = logging.getLogger(__name__)

# The service's own ceiling on records per search; a larger request clamps to this.
_MAX_RESULTS_CEILING = 25
# Applied when the request leaves max_results unset (proto3 zero).
_DEFAULT_MAX_RESULTS = 10
# PMIDs per entity. A variant lookup admits a higher ceiling than keyword search — LitVar2 can
# return thousands and the top-ranked slice is where the signal is — and a PMID costs nothing to
# return: the bibliographic read behind any of them is the caller's own FetchPubmedArticles call.
_VARIANT_MAX_RESULTS_CEILING = 50
_DEFAULT_VARIANT_MAX_RESULTS = 30
# Entities resolved per variant lookup: nothing upstream bounds how many autocomplete returns, and
# each costs a labels fetch plus its own page walk — the fan-out the per-entity budget does not reach.
_MAX_ENTITIES = 8
# A gene's inventory row costs no request of its own: the whole listing arrives in one, so this
# ceiling bounds what a caller reads rather than any fan-out.
_GENE_ENTITIES_CEILING = 200
_DEFAULT_GENE_ENTITIES = 50
# Characters of markdown per read. The budget guards the reading run's context rather than any
# fan-out, so the ceiling is a modest multiple of the default: a run that hits a cut and needs what
# lay past it re-asks for more, and past the ceiling the remainder stays unreachable. The floor is
# what keeps a cut read able to carry its truncation marker within the budget.
_MAX_CHARS_CEILING = 1_000_000
_MAX_CHARS_FLOOR = 1_000
_DEFAULT_MAX_CHARS = 500_000
# The wire value for each verdict on an identifier; an unmapped member raises on lookup.
_AGREEMENT = {
    variants.Agreement.UNCOMPARED: literature_pb2.IdentifierAgreement.AGREEMENT_UNCOMPARED,
    variants.Agreement.AGREES: literature_pb2.IdentifierAgreement.AGREEMENT_AGREES,
    variants.Agreement.DIFFERS: literature_pb2.IdentifierAgreement.AGREEMENT_DIFFERS,
    variants.Agreement.UNSTATED: literature_pb2.IdentifierAgreement.AGREEMENT_UNSTATED,
}

# A readiness batch costs one to two GCS reads per distinct id — two while unsettled, since the sidecar
# probe only short-circuits on a rendering — on the shared default thread executor every other RPC on
# the instance draws from. Server-side because the caller is not the party that bears the cost.
_MAX_DOC_IDS = 100
# The same bound on the external-id path: each id costs a crosswalk row and then a readiness read.
_MAX_EXTERNAL_IDS = 100


class Servicer(literature_pb2_grpc.LiteratureServicer, serving.EvidenceServicer):
    """One bound per rpc: the public method holds the deadline, the work it bounds sits behind it.

    The split is what keeps the bound whole — a handler's validation, its backend calls and the
    response it builds all run inside the budget, so nothing an rpc awaits sits outside it.
    """

    def __init__(
        self, backend: literature_backend.LiteratureBackend, session_resolver: session_mod.SessionResolver
    ) -> None:
        super().__init__(session_resolver)
        self._backend = backend

    @override
    async def DescribePaper(
        self, request: literature_pb2.DescribePaperRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.PaperInfo:
        return await serving.within_deadline(context, 'DescribePaper', self._describe_paper(request, context))

    async def _describe_paper(
        self, request: literature_pb2.DescribePaperRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.PaperInfo:
        try:
            return await self._backend.describe_paper(request.doc_id)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, errors.clipped(f'unknown doc_id {request.doc_id!r}'))

    @override
    async def GetMarkdown(
        self, request: literature_pb2.GetMarkdownRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.GetMarkdownResponse:
        return await serving.within_deadline(context, 'GetMarkdown', self._get_markdown(request, context))

    async def _get_markdown(
        self, request: literature_pb2.GetMarkdownRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.GetMarkdownResponse:
        try:
            return await self._backend.get_markdown(request.doc_id, _clamp_max_chars(request.max_chars))
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, errors.clipped(f'unknown doc_id {request.doc_id!r}'))
        except literature_backend.MissingRenderingBlobError as e:
            await context.abort(grpc.StatusCode.INTERNAL, str(e))

    @override
    async def ResolveContent(
        self, request: literature_pb2.ResolveContentRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ContentLocation:
        return await serving.within_deadline(context, 'ResolveContent', self._resolve_content(request, context))

    async def _resolve_content(
        self, request: literature_pb2.ResolveContentRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ContentLocation:
        selector = _decode_selector(request)
        if selector is None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'ResolveContent requires a selector')
        try:
            return await self._backend.resolve_content(request.doc_id, selector)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, errors.clipped(f'unknown doc_id {request.doc_id!r}'))
        except literature_backend.MissingContentError as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, errors.clipped(str(e)))

    @override
    async def Locate(
        self, request: literature_pb2.LocateRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.LocateResponse:
        return await serving.within_deadline(context, 'Locate', self._locate(request, context))

    async def _locate(
        self, request: literature_pb2.LocateRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.LocateResponse:
        if request.representation not in (literature_pb2.REPRESENTATION_MARKDOWN, literature_pb2.REPRESENTATION_PDF):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'Locate requires a known representation')
        try:
            return await self._backend.locate(request.doc_id, request.quote, request.representation)
        except literature_backend.UnknownPaperError:
            await context.abort(grpc.StatusCode.NOT_FOUND, errors.clipped(f'unknown doc_id {request.doc_id!r}'))
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
        return await serving.within_deadline(context, 'Validate', self._validate(request, context))

    async def _validate(
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
        return await serving.within_deadline(context, 'PollFullTexts', self._poll_full_texts(request, context))

    async def _poll_full_texts(
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
        return await serving.within_deadline(context, 'MaybeIngestPapers', self._maybe_ingest_papers(request, context))

    async def _maybe_ingest_papers(
        self, request: literature_pb2.MaybeIngestPapersRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.MaybeIngestPapersResponse:
        # Bounded on what the request carries, not on what survives dedup: the bound is on the
        # repeated field, and a caller sending 10k ids that collapse to two spent the message budget
        # and this walk regardless.
        if len(request.external_ids) > _MAX_EXTERNAL_IDS:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, f'at most {_MAX_EXTERNAL_IDS} external_ids per request'
            )
        external_ids = list(dict.fromkeys(request.external_ids))
        malformed = [i for i in external_ids if not external_ids_mod.is_qualified(i)]
        if malformed:
            schemes = '/'.join(sorted(external_ids_mod.SCHEMES))
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                errors.clipped(f'external_ids must be scheme-qualified ({schemes}): {malformed}'),
            )
        try:
            lookup_keys = {supplied: external_ids_mod.lookup_key(supplied) for supplied in external_ids}
        except ValueError as e:
            # Clipped: the message echoes a caller field, and an over-limit trailer is dropped whole.
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, errors.clipped(str(e)))
        try:
            doc_ids = await self._backend.resolve_external_ids(list(dict.fromkeys(lookup_keys.values())))
        except literature_backend.CrosswalkNotConfiguredError as e:
            # Permanent for this deployment, so not UNAVAILABLE — gRPC retries that by default, and no
            # number of retries wires a crosswalk.
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, f'id resolution is not configured: {e}')
        except literature_backend.CrosswalkUnavailableError:
            # Whole-call, never a per-id empty doc_id: an outage affects the batch, and a caller
            # reading it per-id would write every one of these papers off as absent from the store.
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
                    doc_id=doc_ids.get(lookup_keys[external_id], ''),
                    state=states.get(
                        doc_ids.get(lookup_keys[external_id], ''), literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER
                    ),
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

    @override
    async def SearchEuropePmc(
        self, request: literature_pb2.SearchEuropePmcRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.SearchEuropePmcResponse:
        return await serving.within_deadline(context, 'SearchEuropePmc', self._search_europe_pmc(request, context))

    async def _search_europe_pmc(
        self, request: literature_pb2.SearchEuropePmcRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.SearchEuropePmcResponse:
        if not request.query.strip():
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'query is required')
        max_results = _clamp_max_results(request.max_results)
        hits = await _discovered(context, self._backend.search_europe_pmc(request.query, max_results))
        return literature_pb2.SearchEuropePmcResponse(
            records=[_record(record) for record in hits.records],
            total_matched=hits.total_matched,
        )

    @override
    async def FetchPubmedArticles(
        self, request: literature_pb2.FetchPubmedArticlesRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.FetchPubmedArticlesResponse:
        return await serving.within_deadline(
            context, 'FetchPubmedArticles', self._fetch_pubmed_articles(request, context)
        )

    async def _fetch_pubmed_articles(
        self, request: literature_pb2.FetchPubmedArticlesRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.FetchPubmedArticlesResponse:
        pmids = await _requested_pmids(request.pmids, context)
        fetched = await _discovered(context, self._backend.fetch_pubmed_articles(pmids))
        return literature_pb2.FetchPubmedArticlesResponse(
            articles=fetched.articles,
            pmids_without_record=fetched.pmids_without_record,
            book_articles=fetched.book_articles,
        )

    @override
    async def SearchLitVar(
        self, request: literature_pb2.SearchLitVarRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.SearchLitVarResponse:
        return await serving.within_deadline(context, 'SearchLitVar', self._search_litvar(request, context))

    async def _search_litvar(
        self, request: literature_pb2.SearchLitVarRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.SearchLitVarResponse:
        try:
            requested = variants.RequestedVariant.of(
                gene=request.gene,
                hgvs_c=request.hgvs_c,
                protein_change=request.protein_change,
                rsid=request.rsid,
                caid=request.caid,
                entity_id=request.entity_id,
            )
        except ValueError as e:
            # Clipped: the message echoes caller identifiers, and an over-limit trailer is dropped whole.
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, errors.clipped(str(e)))
        max_results = _clamp_variant_max_results(request.max_pmids_per_entity)
        found = await _discovered(
            context, self._backend.search_litvar(requested, max_results=max_results, max_entities=_MAX_ENTITIES)
        )
        return literature_pb2.SearchLitVarResponse(
            entities=[_variant_entity(entity) for entity in found.entities],
            total_entities=found.total_entities,
        )

    @override
    async def ListLitVarEntities(
        self, request: literature_pb2.ListLitVarEntitiesRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ListLitVarEntitiesResponse:
        return await serving.within_deadline(
            context, 'ListLitVarEntities', self._list_litvar_entities(request, context)
        )

    async def _list_litvar_entities(
        self, request: literature_pb2.ListLitVarEntitiesRequest, context: grpc.aio.ServicerContext
    ) -> literature_pb2.ListLitVarEntitiesResponse:
        if not request.gene.strip():
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'gene is required')
        max_results = _clamp_gene_entities(request.max_results)
        listed = await _discovered(
            context,
            self._backend.list_litvar_entities(
                gene=request.gene.strip(), contains=request.contains.strip(), max_results=max_results
            ),
        )
        return literature_pb2.ListLitVarEntitiesResponse(
            entities=[
                literature_pb2.ListedEntity(
                    id=entity.id, rsid=entity.rsid, caid=entity.caid, total_records=entity.total_records
                )
                for entity in listed.entities
            ],
            total_in_gene=listed.total_in_gene,
            total_matched=listed.total_matched,
        )


async def _discovered[R](context: grpc.aio.ServicerContext, discovery: Awaitable[R]) -> R:
    """Await one discovery rpc's backend call, mapping an upstream's refusal of the request onto its code.

    An index that refused the request as issued — a non-429 4xx (``errors.raise_for_status``), or
    Europe PMC's HTTP-200 refusal document — judged it, so reissuing it unchanged cannot change the
    answer; that is what the guest's retry helper does with the UNKNOWN an unmapped error becomes.
    """
    try:
        return await discovery
    except errors.InvalidRequestError as e:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))


async def _requested_pmids(requested: Sequence[str], context: grpc.aio.ServicerContext) -> list[str]:
    """The distinct requested PMIDs, normalised, in first-named order — or abort INVALID_ARGUMENT.

    The batch is answered whole or refused: a malformed entry fails the request rather than dropping
    out of the answer, where it would read as a PMID nothing is indexed under — a claim about the
    index rather than about the request.

    Args:
        requested: The identifiers as the request supplied them.
        context: The rpc's context, aborted on any of the three refusals.
    """
    pmids: list[str] = []
    seen: set[str] = set()
    for requested_pmid in requested:
        try:
            pmid = pmids_mod.pmid_key(requested_pmid)
        except ValueError as e:
            # Clipped: the message echoes a caller field, and an over-limit trailer is dropped whole.
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, errors.clipped(str(e)))
        if pmid in seen:
            continue
        seen.add(pmid)
        pmids.append(pmid)
        # Bounded inside the walk, not after it: a request may carry as many entries as the gRPC
        # message limit allows, and this runs on the event loop the whole image shares.
        if len(pmids) > pmids_mod.MAX_PMIDS_PER_BATCH:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'FetchPubmedArticles takes at most {pmids_mod.MAX_PMIDS_PER_BATCH} distinct pmids',
            )
    if not pmids:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'FetchPubmedArticles requires at least one pmid')
    return pmids


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


def _clamp(requested: int, *, default: int, ceiling: int) -> int:
    if requested <= 0:
        return default
    return min(requested, ceiling)


def _clamp_max_results(requested: int) -> int:
    return _clamp(requested, default=_DEFAULT_MAX_RESULTS, ceiling=_MAX_RESULTS_CEILING)


def _clamp_variant_max_results(requested: int) -> int:
    return _clamp(requested, default=_DEFAULT_VARIANT_MAX_RESULTS, ceiling=_VARIANT_MAX_RESULTS_CEILING)


def _clamp_gene_entities(requested: int) -> int:
    return _clamp(requested, default=_DEFAULT_GENE_ENTITIES, ceiling=_GENE_ENTITIES_CEILING)


def _clamp_max_chars(requested: int) -> int:
    return max(_clamp(requested, default=_DEFAULT_MAX_CHARS, ceiling=_MAX_CHARS_CEILING), _MAX_CHARS_FLOOR)


def _variant_entity(entity: variants.VariantEntity) -> literature_pb2.VariantEntity:
    return literature_pb2.VariantEntity(
        id=entity.labels.id,
        rsid=entity.labels.rsid,
        caids=entity.labels.caids,
        genes=entity.labels.genes,
        change=entity.labels.change,
        agreement=literature_pb2.IdentifierAgreement(
            gene=_AGREEMENT[entity.agreement.gene],
            rsid=_AGREEMENT[entity.agreement.rsid],
            caid=_AGREEMENT[entity.agreement.caid],
            change=_AGREEMENT[entity.agreement.change],
        ),
        total_records=entity.total_records,
        pmids=entity.pmids,
    )


def _record(record: europe_pmc.Record) -> literature_pb2.EuropePmcRecord:
    return literature_pb2.EuropePmcRecord(
        pmid=record.pmid,
        title=record.title,
        authors=record.authors,
        journal=record.journal,
        year=record.year,
        doi=record.doi,
        abstract=record.abstract,
        pmcid=record.pmcid,
    )
