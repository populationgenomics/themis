"""The literature interface's backend port (docs/design/literature-evidence-layer.md).

One ``abc.ABC`` for the interface, spanning both of the data boundaries behind it: the full-text
store — naming the GCS object for a paper's rendering, PDF or associated file, serving the canonical
rendering's text, locating a citation's quote within a representation, bringing in a paper it does
not hold, asking for a full text it does not hold yet — and the live indexes — keyword search,
bibliographic records, the LitVar2 entity census. The servicer depends on the port, never on a
concrete backend, so the same server runs offline (``fixture.FixtureBackend``, in memory) and
deployed (``live.LiveBackend``, over GCS, the crosswalk and the public indexes).

Port methods are ``async``: the servicer runs on ``grpc.aio``, so an adapter offloads its blocking
I/O (GCS, anchorite, Cloud SQL) to a thread and issues its HTTP calls on the shared client, rather
than stalling the single event loop.

The pure rule every adapter serves through — which representation is canonical — lives beside the
port, so an offline run answers exactly as a deployed one does.
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import AsyncGenerator, Sequence

from themis.clients.auth import context as auth_context
from themis.litcache import ingest
from themis.rpc import literature_pb2
from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc, pubmed

# The crosswalk key prefix a PubMed identifier is minted under (`themis.litcache.identity`).
_PMID_PREFIX = 'pmid:'

# Re-exported so the servicer can filter on it without reaching into litcache: the schemes an unknown
# paper can be ingested under, which is what the metadata ladder can resolve.
INGESTABLE_SCHEMES = ingest.RESOLVABLE_SCHEMES


class UnknownPaperError(Exception):
    """No paper with the given canonical doc_id — the servicer maps this to NOT_FOUND."""


class MissingContentError(Exception):
    """The paper lacks the selected object (no such rendering / PDF / file) — NOT_FOUND."""


class MissingRenderingBlobError(Exception):
    """The manifest lists a rendering whose bytes the store cannot produce — INTERNAL.

    Distinct from ``MissingContentError``, which is a fact about the paper: here the manifest
    promised the rendering and the store cannot deliver it, so the store has broken its own
    invariant. Reporting it as NOT_FOUND would file a fault as an answer, and NOT_FOUND is the one
    status the shared taxonomy says is never retried.
    """


class CorruptMetadataError(Exception):
    """The paper's ``metadata.pb`` does not read as a ``PaperMetadata`` envelope meeting its constraints — INTERNAL.

    The writer validates every envelope it writes, so bytes that fail to read are the store's own
    fault. An absent ``metadata.pb`` is a paper without metadata and never raises this.
    """


class RepresentationUnavailableError(Exception):
    """The paper has no rendering in the requested representation — FAILED_PRECONDITION."""


class CrosswalkNotConfiguredError(Exception):
    """This deployment wires no crosswalk — the servicer maps this to FAILED_PRECONDITION.

    A permanent property of the deployment, not an outage, so it is deliberately not
    ``CrosswalkUnavailableError``: UNAVAILABLE is retried by gRPC's default policy, and a caller would
    burn its whole retry budget against a call that can never succeed here.
    """


class CrosswalkUnavailableError(Exception):
    """The crosswalk could not be reached — the servicer maps this to UNAVAILABLE.

    Whole-batch by construction, never a per-id miss. A caller that read an outage as "this id is
    not in the store" would write papers off permanently on a transient failure.
    """


class IngestNotConfiguredError(Exception):
    """This deployment cannot ingest a paper — the servicer maps this to FAILED_PRECONDITION.

    Permanent, like ``CrosswalkNotConfiguredError``: without a crosswalk there is nothing to mint
    into, and no number of retries provisions one.
    """


class IngestUnavailableError(Exception):
    """Ingestion could not be carried out, and repeating it might succeed — UNAVAILABLE.

    Whole-batch, never per-id. An upstream that did not answer is not a paper that does not exist:
    reporting it per-id would write the paper off as absent from the corpus for good, and the caller
    would have no reason to ask again. Repeating the call is cheap — a paper whose ids did get minted
    is resolved by the lookup ahead of the ingest next time.
    """


class ConversionNotConfiguredError(Exception):
    """This deployment wires no conversion lane — the servicer maps this to FAILED_PRECONDITION.

    A permanent property of the deployment, like ``CrosswalkNotConfiguredError``: retrying does not
    provision a queue. Raised only when there is something to enqueue, so a deployment that never
    reaches a PENDING paper never sees it.
    """


class ConversionUnavailableError(Exception):
    """The conversion request could not be placed, and repeating it might succeed — UNAVAILABLE.

    UNAVAILABLE because the caller's remedy is to repeat the call, which the ``doc_id``-keyed task
    name makes free for the papers whose tasks did get created. The adapter decides which failures
    qualify.
    """


class ConversionEnqueueFailedError(Exception):
    """The lane is wired and refused the request — the servicer maps this to INTERNAL.

    The distinction from ``ConversionNotConfiguredError`` is what the answer tells the caller. A
    deployment with no conversion lane cannot do this at all, which is a precondition of the call
    here; a deployment whose lane is wired and broken can, and is faulty. Neither is UNAVAILABLE:
    gRPC retries that by default, and no retry repairs either one.
    """


class PdfLocationUnavailableError(Exception):
    """No PDF quote matcher yet — the servicer maps this to UNIMPLEMENTED.

    Distinct from a ``not_located`` result: the quote is not absent, the location cannot be
    computed. A false ``not_located`` would mislabel every PDF quote as unlocatable.
    """


# What object of a paper ResolveContent names — the decoded ResolveContentRequest.selector oneof.
@dataclasses.dataclass(frozen=True)
class MarkdownContent:
    pass


@dataclasses.dataclass(frozen=True)
class PdfContent:
    pass


@dataclasses.dataclass(frozen=True)
class FileContent:
    name: str


ContentSelector = MarkdownContent | PdfContent | FileContent


@dataclasses.dataclass(frozen=True)
class Rendering:
    """A paper's canonical rendering, opened for reading: which bytes they are, then the bytes themselves.

    ``blocks`` yields the rendering's bytes in order, in runs of whatever size the adapter reads, and is
    read once. It holds the store open while it runs, so a reader that stops early closes it
    (``contextlib.aclosing``). It raises ``MissingRenderingBlobError`` if the bytes go missing partway.

    Attributes:
        key: The rendering's record key — the lowercase hex sha256 of its bytes.
        size_bytes: How many bytes ``blocks`` yields in all.
        provenance: How the rendering's text entered the store.
        blocks: The bytes, as they are read.
    """

    key: str
    size_bytes: int
    provenance: literature_pb2.TextProvenance
    blocks: AsyncGenerator[bytes]


@dataclasses.dataclass(frozen=True)
class LiteratureView:
    """The corpus as one call may see it — the route every corpus read takes, and the masking seam.

    The servicer resolves a view from the call's context (``LiteratureBackend.view_for``) and reads the
    corpus through it; the adapters never see a context. Today the view is the whole corpus for every
    call, so each read delegates to the backend unchanged. A masking layer narrows what a call sees
    here — by the Analysis the call is scoped to, never by the person driving it
    (rpc-authorization.md) — without the servicer or an adapter changing.
    """

    _backend: LiteratureBackend

    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        return await self._backend.describe_paper(doc_id)

    async def read_markdown(self, doc_id: str) -> Rendering | literature_pb2.FullTextUnavailable:
        return await self._backend.read_markdown(doc_id)

    async def resolve_content(self, doc_id: str, selector: ContentSelector) -> literature_pb2.ContentLocation:
        return await self._backend.resolve_content(doc_id, selector)

    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        return await self._backend.locate(doc_id, quote, representation)

    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        return await self._backend.validate(doc_id, quote)

    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        return await self._backend.resolve_external_ids(external_ids)

    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        return await self._backend.full_text_readiness(doc_ids)

    async def ingest_papers(self, external_ids: Sequence[str]) -> dict[str, str]:
        """Bring each id this view admits an ingest for into the corpus — see the port's method."""
        return await self._backend.ingest_papers(external_ids)

    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        """Ask for a full text for each of these papers this view admits a conversion for.

        Idempotent: the request is keyed on the ``doc_id``, so repeating it for a paper already in
        flight adds nothing. Callers pass the PENDING ids only — a paper with no manifest has nothing
        for a producer to read, and a settled one needs nothing.

        Raises:
            ConversionNotConfiguredError: this deployment wires no conversion lane (permanent).
            ConversionUnavailableError: the request could not be placed, and repeating it might
                succeed (transient).
            ConversionEnqueueFailedError: the request was refused on its own terms (permanent).
        """
        await self._backend.place_conversions(list(doc_ids))


class LiteratureBackend(abc.ABC):
    @abc.abstractmethod
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        """The paper's representations and files, or raise ``UnknownPaperError``."""
        ...

    @abc.abstractmethod
    async def read_markdown(self, doc_id: str) -> Rendering | literature_pb2.FullTextUnavailable:
        """The paper's canonical rendering, opened for reading whole, or why the store cannot serve it.

        A known paper with no rendering is a ``FullTextUnavailable`` carrying its ``FullTextState`` — a
        fact about the paper, not an error. Opening reads what names the rendering, not its bytes: those
        arrive through ``Rendering.blocks``, so a rendering of any size is served without being held.

        Raises:
            UnknownPaperError: no such doc_id — a broken reference, not a fact about the store.
            MissingRenderingBlobError: the manifest lists a rendering whose bytes are absent.
        """
        ...

    @abc.abstractmethod
    async def resolve_content(self, doc_id: str, selector: ContentSelector) -> literature_pb2.ContentLocation:
        """Name the GCS object for ``selector``.

        Raises:
            UnknownPaperError: no such doc_id.
            MissingContentError: the paper lacks the selected object.
        """
        ...

    @abc.abstractmethod
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        """Locate ``quote`` within ``representation``.

        Returns a ``not_located`` result when the quote is absent (a first-class outcome, not an
        error).

        Raises:
            UnknownPaperError: no such doc_id.
            RepresentationUnavailableError: the paper has no rendering in ``representation``.
            PdfLocationUnavailableError: a PDF quote, which no producer resolves yet.
            MissingRenderingBlobError: the manifest lists the rendering it must read, and the store
                cannot produce its bytes.
        """
        ...

    @abc.abstractmethod
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        """Whether ``quote`` locates in any representation — the agent's authoring-time check.

        Never raises for an unknown doc_id or an absent quote: both are ``ok=false`` with a reason,
        the forgiving answer the agent tool wants.

        Raises:
            MissingRenderingBlobError: the manifest lists the rendering it must read, and the store
                cannot produce its bytes. A fault does not belong in ``ok=false``, which an agent
                reads as a verdict on its quote.
        """
        ...

    @abc.abstractmethod
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        """Look each scheme-qualified external id up in the crosswalk, returning the ids it knows.

        A read, never a mint: this method claims nothing, so it never leaves a ``doc_id`` naming no
        manifest. An id absent from the result is a genuine miss — ``ingest_papers`` is what acts on
        one.

        Args:
            external_ids: Scheme-qualified ``{scheme}:{value}`` keys. The result is a fresh mapping the
                caller owns, so it may extend it with whatever ``ingest_papers`` then brings in.

        Raises:
            CrosswalkNotConfiguredError: this deployment wires no crosswalk (permanent).
            CrosswalkUnavailableError: the crosswalk could not be reached (whole-batch, transient).
        """
        ...

    @abc.abstractmethod
    async def ingest_papers(self, external_ids: Sequence[str]) -> dict[str, str]:
        """Bring each id that names a real paper into the corpus, and return the ``doc_id`` it took.

        Callers pass the ids ``resolve_external_ids`` missed. Each is resolved against its upstream
        first and only then minted, so an id nobody published claims no ``doc_id`` and writes no
        manifest — a mint is permanent, and no re-run undoes one made for a paper that never existed.
        A paper that lands here has ids and bibliographic metadata but no rendering, so it reads
        PENDING and the conversion lane produces its full text.

        Idempotent: an id already minted resolves to the same ``doc_id``, and a paper already
        committed is not rewritten — an id naming one is answered with its ``doc_id`` and claims
        nothing, so the corpus gains no crosswalk row that no manifest names.

        Args:
            external_ids: The scheme-qualified ids ``resolve_external_ids`` did not resolve.

        Returns:
            A mapping of external id to ``doc_id``, holding only the ids that were ingested. An id
            absent from the result named no paper upstream, names a scheme the metadata ladder cannot
            resolve (``litcache.ingest.RESOLVABLE_SCHEMES``), or named a record the store cannot hold
            — all settled facts about that id, never a failure to reach anything. The third is
            reported as the first two are, because readiness has no state for "the index holds this
            and we cannot": a caller is told the store has no such paper, which is true of the store
            and not of the literature. The adapter logs which it was.

        Raises:
            IngestNotConfiguredError: this deployment cannot ingest (permanent).
            IngestUnavailableError: an upstream or the crosswalk could not be reached (whole-batch,
                transient).
        """
        ...

    @abc.abstractmethod
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        """Per-doc_id full-text readiness (READY / PENDING / terminal / unknown).

        Never raises for an unknown doc_id — it is ``FULL_TEXT_STATE_UNKNOWN_PAPER`` in the result, so
        one bad id never fails the batch.
        """
        ...

    def view_for(self, auth: auth_context.AuthContext) -> LiteratureView:
        """The corpus as the call ``auth`` describes may see it — the whole corpus, until masking exists.

        The masking seam (rpc-authorization.md): the call's context reaches the port here and nowhere
        else, so a later narrowing by Analysis happens in one place. Concrete on the port so every
        adapter shares one definition; an adapter with a genuinely different view overrides it.
        """
        del auth
        return LiteratureView(self)

    @abc.abstractmethod
    async def place_conversions(self, doc_ids: Sequence[str]) -> None:
        """Place the conversion requests for these papers: the adapter's enqueue, reached through a view.

        Called through a view, which has already admitted these doc_ids; at most one conversion in
        flight per paper. Whole-batch failures, never per-id: a caller cannot act on "three of these ten
        were placed", and repeating the whole call costs nothing for the ones that were. Raises the
        ``Conversion*`` failures ``LiteratureView.request_conversions`` documents.
        """
        ...

    @abc.abstractmethod
    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        """Bibliographic records matching ``query``, most-relevant first, with the index's census.

        At most ``max_results`` records come back; ``total_matched`` counts every record the query
        matched, so a list the budget cut is legible as the prefix it is.
        """
        ...

    @abc.abstractmethod
    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        """Each requested PMID's outcome: a journal record, a book record, or nothing indexed.

        ``pmids`` are already keyed (``pmids.pmid_key``) and distinct. The answer is total over them:
        every requested PMID lands in exactly one of ``articles`` and ``book_articles`` (each keyed
        by the PMID its record carries) or ``pmids_without_record`` — an omission would put the
        caller back to inferring absence from a gap.

        No outcome is a fault: a lookup the backend cannot complete raises, so a record reported
        absent was looked for and not found.
        """
        ...

    @abc.abstractmethod
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        """Every LitVar2 entity the request's identifiers reached, in resolution order.

        Each entity carries the index's own labels, the per-identifier verdict on how they line up
        with the request, its whole publication count, and its top-ranked PMIDs up to
        ``max_results`` — a per-entity budget, never one shared across entities. An entity whose
        labels disagree with the request is returned with the disagreement stated, never dropped:
        the service cannot tell a wrong request from an upstream mislabelling.

        Resolution keeps at most ``max_entities``, since each one costs its own upstream round trips;
        the returned ``total_entities`` says how many were reached, so a cut set is legible as one.

        An empty entity set means the index holds no entity for these identifiers (a valid result),
        distinct from a transport/parse failure, which raises.
        """
        ...

    @abc.abstractmethod
    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        """A gene's LitVar2 entity inventory, most-published first, with its census.

        ``contains`` keeps the entities whose id's change segment — its last ``#``-separated part,
        the one place the listing states a change — holds it as a case-insensitive substring. At
        most ``max_results`` rows come back, and the census reports how many the gene has and how
        many ``contains`` kept, so a prefix is legible as one. A gene the index holds nothing for
        yields empty rows and a zero census — a fact.
        """
        ...


def default_representation(has_markdown: bool, markdown_from_xml: bool, has_pdf: bool) -> literature_pb2.Representation:
    """Markdown when a high-fidelity source-XML rendering exists; else the PDF; else the markdown."""
    if has_markdown and markdown_from_xml:
        return literature_pb2.REPRESENTATION_MARKDOWN
    if has_pdf:
        return literature_pb2.REPRESENTATION_PDF
    if has_markdown:
        return literature_pb2.REPRESENTATION_MARKDOWN
    return literature_pb2.REPRESENTATION_UNSPECIFIED
