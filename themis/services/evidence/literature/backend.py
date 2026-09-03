"""The literature interface's backend port (docs/design/literature-evidence-layer.md).

One ``abc.ABC`` for the interface, spanning both of the data boundaries behind it: the full-text
store — naming the GCS object for a paper's rendering, PDF or associated file, serving the canonical
rendering's text, locating a citation's quote within a representation, asking for a full text it does
not hold yet — and the live indexes — keyword search, bibliographic records, the LitVar2 entity
census. The servicer depends on the port, never on a concrete backend, so the same server runs
offline (``fixture.FixtureBackend``, in memory) and deployed (``live.LiveBackend``, over GCS, the
crosswalk and the public indexes).

Port methods are ``async``: the servicer runs on ``grpc.aio``, so an adapter offloads its blocking
I/O (GCS, anchorite, Cloud SQL) to a thread and issues its HTTP calls on the shared client, rather
than stalling the single event loop.

The pure rules every adapter serves through — the per-read markdown budget, the canonical
representation — live beside the port, so an offline run is bounded exactly as a deployed one is.
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Sequence

from themis.rpc import literature_pb2
from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc, pubmed

# The crosswalk key prefix a PubMed identifier is minted under (`themis.litcache.identity`).
_PMID_PREFIX = 'pmid:'

# Beyond the read's character budget the text is cut at a line boundary and carries this marker, so a
# reader can tell a whole paper from a clipped one — and cannot quote past the cut.
_TRUNCATION_MARKER = (
    '\n\n---\n\n**[Capture truncated: the full text exceeded {budget} characters and '
    '{dropped} were dropped. Nothing beyond this point is part of the captured snapshot, so it '
    'cannot be quoted or cited.]**'
)


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


class LiteratureBackend(abc.ABC):
    @abc.abstractmethod
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        """The paper's representations and files, or raise ``UnknownPaperError``."""
        ...

    @abc.abstractmethod
    async def get_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        """The paper's canonical rendering as markdown, or why the store cannot serve it.

        A known paper with no rendering is the ``unavailable`` arm carrying its ``FullTextState`` — a
        fact about the paper, not an error. ``max_chars`` is the already-clamped budget; the text is
        served through ``capped_markdown``, with the rendering's whole ``total_chars`` beside it.

        Raises:
            UnknownPaperError: no such doc_id — a broken reference, not a fact about the store.
            MissingRenderingBlobError: the manifest lists a rendering whose text is absent.
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

        A read, never a mint: minting *claims*, so an id the store has never ingested would take a
        ``doc_id`` naming no manifest. An id absent from the result is a genuine miss.

        Raises:
            CrosswalkNotConfiguredError: this deployment wires no crosswalk (permanent).
            CrosswalkUnavailableError: the crosswalk could not be reached (whole-batch, transient).
        """
        ...

    @abc.abstractmethod
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        """Per-doc_id full-text readiness (READY / PENDING / terminal / unknown).

        Never raises for an unknown doc_id — it is ``FULL_TEXT_STATE_UNKNOWN_PAPER`` in the result, so
        one bad id never fails the batch.
        """
        ...

    @abc.abstractmethod
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        """Ask for a full text for each of these papers, at most one conversion in flight per paper.

        Idempotent: the request is keyed on the ``doc_id``, so repeating it for a paper already in
        flight adds nothing. Callers pass the PENDING ids only — a paper with no manifest has nothing
        for a producer to read, and a settled one needs nothing.

        Whole-batch failures, never per-id: a caller cannot act on "three of these ten were placed",
        and repeating the whole call costs nothing for the ones that were.

        Raises:
            ConversionNotConfiguredError: this deployment wires no conversion lane (permanent).
            ConversionUnavailableError: the request could not be placed, and repeating it might
                succeed (transient).
            ConversionEnqueueFailedError: the request was refused on its own terms (permanent).
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


def capped_markdown(markdown: str, max_chars: int) -> tuple[str, int]:
    """``markdown`` within ``max_chars``, and the whole rendering's character count.

    ``max_chars`` is the budget the servicer already clamped, never a caller's raw ask. The count is
    of the rendering as a whole, so a caller tells a clipped read from a complete one by comparing it
    against the text's own length, and learns how much lies past the cut. The cut lands on a line
    boundary and carries a marker naming it — inside the budget, so the comparison stays sound — and
    what the cut removed cannot be quoted. Every backend serves through this, so what an offline run
    may quote is bounded exactly as a deployed one is.

    Raises:
        ValueError: ``max_chars`` is too small to carry the truncation marker; the servicer's floor
            keeps a caller's budget above this.
    """
    total_chars = len(markdown)
    if total_chars <= max_chars:
        return markdown, total_chars
    # The marker rides inside the budget, so the returned text never exceeds it — which is what lets
    # a reader test "came whole" as total_chars against the text's own length. `dropped` is bounded
    # by total_chars, so reserving for that spelling reserves enough for the real one.
    reserve = len(_TRUNCATION_MARKER.format(budget=max_chars, dropped=total_chars))
    if max_chars <= reserve:
        raise ValueError(f'max_chars {max_chars} cannot carry the truncation marker ({reserve} chars)')
    cut = markdown.rfind('\n', 0, max_chars - reserve)
    if cut <= 0:
        cut = max_chars - reserve
    kept = markdown[:cut].rstrip()
    marker = _TRUNCATION_MARKER.format(budget=max_chars, dropped=total_chars - len(kept))
    return kept + marker, total_chars


def default_representation(has_markdown: bool, markdown_from_xml: bool, has_pdf: bool) -> literature_pb2.Representation:
    """Markdown when a high-fidelity source-XML rendering exists; else the PDF; else the markdown."""
    if has_markdown and markdown_from_xml:
        return literature_pb2.REPRESENTATION_MARKDOWN
    if has_pdf:
        return literature_pb2.REPRESENTATION_PDF
    if has_markdown:
        return literature_pb2.REPRESENTATION_MARKDOWN
    return literature_pb2.REPRESENTATION_UNSPECIFIED
