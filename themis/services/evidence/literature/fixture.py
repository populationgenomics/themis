"""The offline literature backend: one in-memory adapter over an explicitly seeded corpus and index.

``FixtureBackend`` serves the whole port from data a caller states outright — seeded papers for the
store half, seeded bibliographic records and LitVar2 entities for the index half. Nothing here
reaches the network or a bucket, so a run against it is reproducible and needs no cloud access.

The seed format and its parser live here with the dataclasses they build: the seed is this backend's
input schema, and a caller names the source it read the JSON from so every rejection points at the
value to fix. The two vocabularies are parsed separately — papers and index are unrelated shapes —
and a section that does not match its schema is a ``SystemExit`` at startup, never a backend that
serves an empty half.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import re
from collections.abc import Mapping, Sequence
from typing import NamedTuple, override

import anchorite
from pubmed_proto import pubmed_pb2

from themis.litcache import crosswalk
from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import external_ids as external_ids_mod
from themis.services.evidence.literature import pmids as pmids_mod
from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc, litvar, pubmed

_logger = logging.getLogger(__name__)


class _StoreSeedError(ValueError):
    """Seeded papers the fixture cannot stand up on — attributed to the store section."""


class _IndexSeedError(ValueError):
    """A seeded index the fixture cannot stand up on — attributed to the discovery section."""


@dataclasses.dataclass(frozen=True)
class SeededFile:
    name: str
    role: literature_pb2.FileRole
    media_type: str
    gcs_uri: str


@dataclasses.dataclass(frozen=True)
class SeededPdfLocation:
    page: int  # 0-based
    rects: tuple[tuple[float, float, float, float], ...]  # (x, y, width, height), page fractions


@dataclasses.dataclass(frozen=True)
class SeededPaper:
    """One paper's fixture data: its representations, files, and seeded PDF quote locations.

    Markdown quote answers are never seeded: locate and validate derive them from
    markdown_text through the live matcher, so they cannot contradict the text a read serves.

    ``readiness`` is the terminal state of a paper with no rendering (NO_FULL_TEXT or FAILED);
    everything else is derived, as the store derives it from the litcache layout.
    """

    title: str
    external_ids: tuple[str, ...] = ()  # scheme-qualified, as the crosswalk keys them
    files: tuple[SeededFile, ...] = ()
    markdown_gcs_uri: str | None = None
    markdown_from_xml: bool = False
    markdown_text: str | None = None
    pdf_gcs_uri: str | None = None
    provenance: literature_pb2.TextProvenance = literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS
    readiness: literature_pb2.FullTextState | None = None
    pdf_locations: Mapping[str, SeededPdfLocation] = dataclasses.field(default_factory=dict)


class SeededEntity(NamedTuple):
    """One seeded LitVar2 entity: its labels, its ranked PMIDs, and the count it claims whole.

    ``total_records`` above ``len(pmids)`` seeds an entity the index links more records to than the
    seed lists — the census a caller has to be able to read against the listed PMIDs.
    """

    labels: litvar.EntityLabels
    pmids: tuple[str, ...]
    total_records: int


class SeededBook(NamedTuple):
    """One seeded book record — a GeneReviews chapter, say — as ``fetch_pubmed_articles`` answers it.

    ``nbk`` is the Bookshelf accession (``NBK…``) PubMed carries beside the PMID; ``title`` is the
    chapter's and ``book_title`` the book's.
    """

    pmid: str
    nbk: str
    title: str
    book_title: str
    publisher: str
    contribution_date: datetime.date
    date_revised: datetime.date
    authors: tuple[str, ...] = ()
    abstract: str = ''


class FixtureBackend(literature_backend.LiteratureBackend):
    """The whole port in memory, over an explicit set of papers, records and entities.

    The store half answers from the ``SeededPaper``s; the index half answers from the seeded records
    and entities. ``search_europe_pmc`` returns the records whose text matches the query as a
    case-insensitive substring (all of them for an empty query), clamped to ``max_results`` and
    counted whole in ``total_matched``; ``search_litvar`` keeps the entities an identifier of the
    request names, each with its seeded PMIDs; ``list_litvar_entities`` lists the entities of a
    gene; ``fetch_pubmed_articles`` reads the seeded records by PMID as whole ``PubmedArticle``s —
    one with no seeded abstract is the index stating the record has none — and the seeded books as
    whole ``PubmedBookArticle``s; a PMID neither carries lands in ``pmids_without_record``.

    Entity resolution here is exact, where LitVar2's autocomplete is fuzzy: an entity is reached when
    one of the request's identifiers keys it, and never merely because it looks similar. A seed that
    means to exercise a disagreement states it outright — an entity keyed on the requested rsID whose
    gene label is another gene's.

    Raises:
        _StoreSeedError: Two papers claim one external id.
        _IndexSeedError: Two records or books claim one PMID, or an entity lists a PMID none of
            them carries.

    Each is a state the real store or index cannot be in, and each would have a paper or a record
    silently leave the corpus — a lookup coming back short with the census still counting it. Both
    are ``ValueError``s, so a caller building one by hand can catch that; the two types are how
    ``backend_from_seed`` tells which section to name, and collapsing them back into one plain
    ``ValueError`` would leave a failure attributed to whichever section is convenient.
    """

    def __init__(
        self,
        papers: Mapping[str, SeededPaper],
        records: Sequence[europe_pmc.Record],
        entities: Sequence[SeededEntity],
        *,
        book_articles: Sequence[SeededBook] = (),
    ) -> None:
        self._papers = dict(papers)
        self._crosswalk = _index_by_external_id(self._papers)
        self._records = list(records)
        self._by_pmid: dict[str, europe_pmc.Record] = {}
        for record in self._records:
            if not record.pmid:
                continue  # a hit the index carries under no PubMed id: searchable, never fetched by one
            if record.pmid in self._by_pmid:
                raise _IndexSeedError(f'pmid {record.pmid!r} is seeded twice; one identifier names one record')
            if record.year and not (record.year.isdecimal() and 1 <= int(record.year) <= 9999):
                raise _IndexSeedError(
                    f'record {record.pmid!r} states year {record.year!r}; its PubMed view carries a date, '
                    'which needs a plain year'
                )
            self._by_pmid[record.pmid] = record
        self._books: dict[str, SeededBook] = {}
        for book in book_articles:
            if book.pmid in self._books:
                raise _IndexSeedError(f'pmid {book.pmid!r} is seeded as two books; one identifier names one record')
            self._books[book.pmid] = book
        if twice := sorted(self._books.keys() & self._by_pmid.keys()):
            raise _IndexSeedError(f'pmid(s) {twice} seeded as both a record and a book')
        self._entities = list(entities)
        for seeded in self._entities:
            missing = [pmid for pmid in seeded.pmids if pmid not in self._by_pmid and pmid not in self._books]
            if missing:
                raise _IndexSeedError(
                    f'seeded entity {seeded.labels.id!r} lists PMIDs no seeded record or book carries: {missing}'
                )

    def _paper(self, doc_id: str) -> SeededPaper:
        try:
            return self._papers[doc_id]
        except KeyError:
            raise literature_backend.UnknownPaperError(doc_id) from None

    @override
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        paper = self._paper(doc_id)
        has_markdown = paper.markdown_gcs_uri is not None
        has_pdf = paper.pdf_gcs_uri is not None
        return literature_pb2.PaperInfo(
            doc_id=doc_id,
            title=paper.title,
            has_markdown=has_markdown,
            markdown_from_xml=paper.markdown_from_xml,
            has_pdf=has_pdf,
            default_representation=literature_backend.default_representation(
                has_markdown, paper.markdown_from_xml, has_pdf
            ),
            files=[literature_pb2.FileInfo(name=f.name, role=f.role, media_type=f.media_type) for f in paper.files],
        )

    @override
    async def get_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        paper = self._paper(doc_id)
        if paper.markdown_gcs_uri is None:
            return literature_pb2.GetMarkdownResponse(
                unavailable=literature_pb2.FullTextUnavailable(state=_seeded_readiness(paper))
            )
        markdown, total_chars = literature_backend.capped_markdown(self._rendering_text(doc_id, paper), max_chars)
        return literature_pb2.GetMarkdownResponse(
            content=literature_pb2.PaperMarkdown(
                doc_id=doc_id,
                markdown=markdown,
                provenance=paper.provenance,
                total_chars=total_chars,
            )
        )

    @override
    async def resolve_content(
        self, doc_id: str, selector: literature_backend.ContentSelector
    ) -> literature_pb2.ContentLocation:
        paper = self._paper(doc_id)
        match selector:
            case literature_backend.MarkdownContent():
                if paper.markdown_gcs_uri is None:
                    raise literature_backend.MissingContentError(f'{doc_id} has no markdown rendering')
                return literature_pb2.ContentLocation(gcs_uri=paper.markdown_gcs_uri, media_type='text/markdown')
            case literature_backend.PdfContent():
                if paper.pdf_gcs_uri is None:
                    raise literature_backend.MissingContentError(f'{doc_id} has no PDF')
                return literature_pb2.ContentLocation(gcs_uri=paper.pdf_gcs_uri, media_type='application/pdf')
            case literature_backend.FileContent(name=name):
                for f in paper.files:
                    if f.name == name:
                        return literature_pb2.ContentLocation(gcs_uri=f.gcs_uri, media_type=f.media_type)
                raise literature_backend.MissingContentError(f'{doc_id} has no file {name!r}')

    def _rendering_text(self, doc_id: str, paper: SeededPaper) -> str:
        """The seeded rendering's text — reads, locates and validations all run over this one copy."""
        if paper.markdown_text is None:
            raise literature_backend.MissingRenderingBlobError(
                f'{doc_id} has a markdown rendering the seed gives no text for'
            )
        return paper.markdown_text

    @override
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        paper = self._paper(doc_id)
        if representation == literature_pb2.REPRESENTATION_MARKDOWN:
            if paper.markdown_gcs_uri is None:
                raise literature_backend.RepresentationUnavailableError(f'{doc_id} has no markdown rendering')
            offsets = await asyncio.to_thread(anchorite.locate_quote_span, self._rendering_text(doc_id, paper), quote)
            if offsets is None:
                return literature_pb2.LocateResponse(not_located=literature_pb2.QuoteNotLocated())
            start, end = offsets
            return literature_pb2.LocateResponse(offsets=literature_pb2.TextOffsets(start=start, end=end))
        if representation == literature_pb2.REPRESENTATION_PDF:
            if paper.pdf_gcs_uri is None:
                raise literature_backend.RepresentationUnavailableError(f'{doc_id} has no PDF')
            location = paper.pdf_locations.get(quote)
            if location is None:
                return literature_pb2.LocateResponse(not_located=literature_pb2.QuoteNotLocated())
            return literature_pb2.LocateResponse(region=_pdf_region(location))
        raise ValueError(f'unsupported representation {representation!r}')

    @override
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        paper = self._papers.get(doc_id)
        if paper is None:
            return literature_pb2.ValidateResponse(ok=False, reason=f'unknown doc_id {doc_id!r}')
        located_in: list[literature_pb2.Representation] = []
        if (
            paper.markdown_gcs_uri is not None
            and await asyncio.to_thread(anchorite.locate_quote_span, self._rendering_text(doc_id, paper), quote)
            is not None
        ):
            located_in.append(literature_pb2.REPRESENTATION_MARKDOWN)
        if quote in paper.pdf_locations:
            located_in.append(literature_pb2.REPRESENTATION_PDF)
        if not located_in:
            return literature_pb2.ValidateResponse(ok=False, reason='quote not located in any representation')
        return literature_pb2.ValidateResponse(ok=True, located_in=located_in)

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        return {
            external_id: self._crosswalk[folded]
            for external_id, folded in ((i, crosswalk.normalise_key(i)) for i in dict.fromkeys(external_ids))
            if folded in self._crosswalk
        }

    @override
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        # The seed models no conversion queue, so a PENDING paper never advances.
        result: dict[str, literature_pb2.FullTextState] = {}
        for doc_id in doc_ids:
            paper = self._papers.get(doc_id)
            result[doc_id] = literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER if paper is None else _seeded_readiness(paper)
        return result

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        # The seed has no queue and no worker, so a PENDING paper stays PENDING however often it is
        # asked for. Logged rather than passed over in silence: an offline caller watching a paper
        # never advance has to be able to see that nothing was ever going to convert it.
        if doc_ids:
            _logger.info('fixture backend converts nothing; %d paper(s) stay PENDING: %s', len(doc_ids), list(doc_ids))

    @override
    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        needle = query.strip().casefold()
        matches = [record for record in self._records if not needle or needle in _haystack(record)]
        return europe_pmc.SearchHits(records=matches[:max_results], total_matched=len(matches))

    @override
    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        return pubmed.FetchedArticles(
            articles=[_pubmed_article(self._by_pmid[pmid]) for pmid in pmids if pmid in self._by_pmid],
            book_articles=[_pubmed_book_article(self._books[pmid]) for pmid in pmids if pmid in self._books],
            pmids_without_record=[pmid for pmid in pmids if pmid not in self._by_pmid and pmid not in self._books],
        )

    @override
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        by_rank: list[tuple[int, SeededEntity]] = []
        for seeded in self._entities:
            rank = _seeded_entity_rank(seeded.labels, requested)
            # names_an_allele belongs to the port's contract, not to one adapter: a seeded gene-level
            # entity must answer here exactly as the index's own does.
            if rank is not None and seeded.labels.names_an_allele():
                by_rank.append((rank, seeded))
        by_rank.sort(key=lambda pair: pair[0])  # stable: seed order holds within one identifier's rank
        reached = [seeded for _, seeded in by_rank]
        resolved = reached[:max_entities]
        return variants.VariantCensus(
            entities=tuple(
                variants.VariantEntity(
                    labels=seeded.labels,
                    agreement=variants.identifier_agreement(requested, seeded.labels),
                    total_records=seeded.total_records,
                    pmids=seeded.pmids[:max_results],
                )
                for seeded in resolved
            ),
            total_entities=len(reached),
        )

    @override
    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        wanted = gene.casefold()
        listed = [
            litvar.ListedEntity(
                id=seeded.labels.id,
                rsid=seeded.labels.rsid,
                caid=seeded.labels.caids[0] if seeded.labels.caids else '',
                total_records=seeded.total_records,
            )
            for seeded in self._entities
            if any(symbol.casefold() == wanted for symbol in seeded.labels.genes)
        ]
        return variants.gene_inventory(listed, contains=contains, max_results=max_results)


def _seeded_readiness(paper: SeededPaper) -> literature_pb2.FullTextState:
    """A seeded paper's readiness, derived as ``litcache.outcome`` derives it from the layout.

    A rendering is text to serve; a seeded terminal state stands in for the ``.fetch_outcome``
    marker; anything else has not settled.
    """
    if paper.markdown_gcs_uri is not None:
        return literature_pb2.FULL_TEXT_STATE_READY
    if paper.readiness is not None:
        return paper.readiness
    return literature_pb2.FULL_TEXT_STATE_PENDING


def _index_by_external_id(papers: Mapping[str, SeededPaper]) -> dict[str, str]:
    """The doc_id each seeded external id names, folded as the live crosswalk folds its rows.

    The seed spells ids as the crosswalk keys them, and a caller may spell them any way the scheme
    allows, so both sides fold.

    Raises:
        _StoreSeedError: two papers claim one external id — the crosswalk's primary key cannot hold
            that, so neither can a fixture standing in for it. The type, not just its ``ValueError``
            base, is what attributes the failure to the store section.
    """
    index: dict[str, str] = {}
    for doc_id, paper in papers.items():
        for external_id in paper.external_ids:
            incumbent = index.setdefault(crosswalk.normalise_key(external_id), doc_id)
            if incumbent != doc_id:
                raise _StoreSeedError(f'external id {external_id!r} is claimed by both {incumbent!r} and {doc_id!r}')
    return index


def _pdf_region(location: SeededPdfLocation) -> literature_pb2.PdfRegion:
    return literature_pb2.PdfRegion(
        page=location.page,
        rects=[literature_pb2.Rect(x=x, y=y, width=w, height=h) for (x, y, w, h) in location.rects],
    )


def _seeded_entity_rank(labels: litvar.EntityLabels, requested: variants.RequestedVariant) -> int | None:
    """Which of the request's identifiers keys this seeded entity, or ``None`` where none does.

    The rank is the identifier's place in ``variants.litvar_queries``' order, so the fixture reports
    entities in the order the real backend's queries reach them.
    """
    if requested.entity_id:
        return 0 if labels.id == requested.entity_id else None
    agreement = variants.identifier_agreement(requested, labels)
    if agreement.caid is variants.Agreement.AGREES:
        return 0
    if agreement.rsid is variants.Agreement.AGREES:
        return 1
    if agreement.gene is variants.Agreement.AGREES and agreement.change is variants.Agreement.AGREES:
        return 2
    return None


def _haystack(record: europe_pmc.Record) -> str:
    return f'{record.title} {record.abstract} {" ".join(record.authors)} {record.journal}'.casefold()


def _pubmed_article(record: europe_pmc.Record) -> pubmed_pb2.PubmedArticle:
    """The seeded record as a whole ``PubmedArticle``, filling the fields a triage read consumes."""
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.pmid.value = record.pmid
    article.medline_citation.article.article_title.value = record.title
    article.medline_citation.article.journal.title = record.journal
    if record.year:
        article.medline_citation.article.journal.journal_issue.pub_date.FromDatetime(
            datetime.datetime(int(record.year), 1, 1, tzinfo=datetime.UTC)
        )
    if record.abstract:
        article.medline_citation.article.abstract.abstract_text.add(value=record.abstract)
    for name in record.authors:
        # The seed states one display name per author; it rides whole rather than being split into
        # name parts the seed never stated.
        article.medline_citation.article.author_list.author.add(last_name=name)
    if record.doi:
        article.pubmed_data.article_id_list.add(id_type=pubmed_pb2.ArticleId.ID_TYPE_DOI, value=record.doi)
    if record.pmcid:
        article.pubmed_data.article_id_list.add(id_type=pubmed_pb2.ArticleId.ID_TYPE_PMC, value=record.pmcid)
    return article


def _pubmed_book_article(book: SeededBook) -> pubmed_pb2.PubmedBookArticle:
    """The seeded book as a whole ``PubmedBookArticle``, filling the fields a triage read consumes."""
    record = pubmed_pb2.PubmedBookArticle()
    document = record.book_document
    document.pmid.value = book.pmid
    document.article_id_list.add(id_type=pubmed_pb2.ArticleId.ID_TYPE_BOOKACCESSION, value=book.nbk)
    document.book.publisher.publisher_name = book.publisher
    document.book.book_title.value = book.book_title
    document.article_title.value = book.title
    if book.authors:
        authors = document.author_list.add(type=pubmed_pb2.AuthorList.TYPE_AUTHORS)
        for name in book.authors:
            authors.author.add(last_name=name)  # one display name per author, whole, as for a record
    if book.abstract:
        document.abstract.abstract_text.add(value=book.abstract)
    document.contribution_date.FromDatetime(_midnight_utc(book.contribution_date))
    document.date_revised.FromDatetime(_midnight_utc(book.date_revised))
    return record


def _midnight_utc(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(), tzinfo=datetime.UTC)


# --- Seed parsing: JSON to the backend above ------------------------------------------------------

_FILE_ROLES = {
    'FIGURE': literature_pb2.FILE_ROLE_FIGURE,
    'SUPPLEMENTARY': literature_pb2.FILE_ROLE_SUPPLEMENTARY,
}

_PROVENANCES = {
    'OPEN_ACCESS': literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS,
    'SUPPLIED': literature_pb2.TEXT_PROVENANCE_SUPPLIED,
}

# Only the terminal states are seedable: READY follows from a rendering, PENDING is the absence of
# both, and UNKNOWN_PAPER is a paper the store does not hold at all — no seed can state the three.
_TERMINAL_READINESS = {
    'NO_FULL_TEXT': literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT,
    'FAILED': literature_pb2.FULL_TEXT_STATE_FAILED,
}

_PAPER_FIELDS = frozenset(
    {
        'title',
        'external_ids',
        'files',
        'markdown',
        'pdf',
        'pdf_locations',
        'provenance',
        'readiness',
    }
)

_INDEX_FIELDS = frozenset({'records', 'entities', 'book_articles'})
_RECORD_FIELDS = frozenset({'pmid', 'title', 'authors', 'journal', 'year', 'doi', 'abstract', 'pmcid'})
_BOOK_FIELDS = frozenset(
    {'pmid', 'nbk', 'title', 'book_title', 'publisher', 'authors', 'contribution_date', 'date_revised', 'abstract'}
)
_BOOK_ACCESSION = re.compile(r'NBK[0-9]+')
_ENTITY_FIELDS = frozenset({'id', 'rsid', 'caids', 'genes', 'change', 'pmids', 'total_records'})


def backend_from_seed(store: object, index: object, *, store_source: str, index_source: str) -> FixtureBackend:
    """Build a ``FixtureBackend`` from the two decoded JSON sections, or ``SystemExit``.

    The store section is a JSON object mapping each canonical doc_id to a paper:

        {"<doc_id>": {
            "title": "...",
            "external_ids": ["pmid:31234567", "doi:10.1/x"],
            "provenance": "OPEN_ACCESS" | "SUPPLIED",               // optional
            "readiness": "NO_FULL_TEXT" | "FAILED",                 // optional
            "markdown": {"gcs_uri": "gs://...", "from_xml": true,
                         "text": "# ..."},                          // optional
            "pdf": {"gcs_uri": "gs://..."},                          // optional
            "files": [{"name": "f1.png", "role": "FIGURE", "media_type": "image/png",
                       "gcs_uri": "gs://..."}],
            "pdf_locations": {"<quote>": {"page": 0, "rects": [[x, y, w, h]]}}
        }}

    ``provenance`` defaults to ``OPEN_ACCESS`` — the disposition of all but a deposited paper, and
    the one a seed that says nothing means. ``readiness`` states why a paper without a rendering has
    no text; a paper seeding both it and a ``markdown`` is rejected, since the rendering already says
    the text is there, as is one seeding locations in a representation it has no rendering for.
    ``markdown.text`` is what ``GetMarkdown`` serves, under the same per-read budget the deployed
    store serves under; a ``markdown`` without one is a paper the store lists a rendering for but
    cannot produce the text of.

    The index section is one JSON object with three lists:

        {"records": [{"pmid": "31234567", "title": "...", "authors": ["Xu W", "Yang X"],
                      "journal": "...", "year": "2019", "doi": "10.1/x", "abstract": "..."}],
         "entities": [{"id": "litvar@rs00##", "rsid": "rs00", "caids": ["CA1000"],
                       "genes": ["GENE1"], "change": "c.1063G>A",
                       "pmids": ["31234567"], "total_records": 5}],
         "book_articles": [{"pmid": "20301288", "nbk": "NBK1116", "title": "...",
                            "book_title": "...", "publisher": "...", "authors": ["Xu W"],
                            "contribution_date": "2010-03-23", "date_revised": "2024-01-04",
                            "abstract": "..."}]}

    Every record field but ``pmid`` is optional and defaults to what the index states for a record
    carrying none — the empty string, or for ``authors`` the empty list. An absent ``abstract`` is
    the index stating the record has none. An entity's ``change`` is in the index's own notation
    (``c.1063G>A``, ``p.A355T``), its ``pmids`` are in relevance order, and ``total_records`` absent
    means the entity holds exactly the listed PMIDs; below that length is an operator error. A book
    is a PMID PubMed answers with a book record — a GeneReviews chapter, say — and states its
    bibliography whole: every field but ``authors`` and ``abstract`` is required, ``nbk`` is the
    Bookshelf accession, and the two dates are ISO ``YYYY-MM-DD``. A PMID seeded as both a record
    and a book is rejected. An entity listing a PMID neither list seeds is rejected: a dropped PMID
    would shorten a lookup's list without the census saying so.

    An unknown field is rejected rather than dropped: a typo'd key would otherwise seed a paper or a
    record missing exactly the data the test or deploy meant to give it.

    Args:
        store: The decoded store section. ``{}`` is an explicit empty store.
        index: The decoded discovery section. ``{"records": [], "entities": []}`` is an explicit
            empty index.
        store_source: Where ``store`` came from — an env var and its section — quoted in every
            failure message about it.
        index_source: The same for ``index``.

    Returns:
        A backend over the seeded papers and index.

    Raises:
        SystemExit: a section does not match the schema above. A document broken in both is reported
            against the store, so an operator fixes the sections in the order they are written.
    """
    papers = _papers_from_seed(store, source=store_source)
    try:
        # Redundant with the constructor's own check below, and ordered: a document broken in both
        # sections names the store, the section a reader fixes first.
        _index_by_external_id(papers)
    except _StoreSeedError as e:
        raise SystemExit(f'{store_source} {e}') from e
    records, entities, books = _index_from_seed(index, source=index_source)
    try:
        return FixtureBackend(papers, records, entities, book_articles=books)
    except _StoreSeedError as e:
        raise SystemExit(f'{store_source} {e}') from e
    except _IndexSeedError as e:
        raise SystemExit(f'{index_source} {e}') from e


def _papers_from_seed(seed: object, *, source: str) -> dict[str, SeededPaper]:
    if not isinstance(seed, dict):
        raise SystemExit(f'{source} must be a JSON object of doc_id -> paper, got {type(seed).__name__}')
    return {doc_id: _parse_paper(source, doc_id, paper) for doc_id, paper in seed.items()}


def _index_from_seed(
    seed: object, *, source: str
) -> tuple[list[europe_pmc.Record], list[SeededEntity], list[SeededBook]]:
    if not isinstance(seed, dict):
        raise SystemExit(
            f'{source} must be a JSON object of "records", "entities" and "book_articles", got {type(seed).__name__}'
        )
    unknown = set(seed) - _INDEX_FIELDS
    if unknown:
        raise SystemExit(f'{source} has unknown field(s) {sorted(unknown)}')
    missing = sorted(_INDEX_FIELDS - set(seed))
    if missing:
        raise SystemExit(f'{source} is missing field(s) {missing}; seed an empty array to mean "nothing here"')
    records = [_parse_record(source, entry) for entry in _index_list(source, 'records', seed['records'])]
    entities = [_parse_entity(source, entry) for entry in _index_list(source, 'entities', seed['entities'])]
    books = [_parse_book(source, entry) for entry in _index_list(source, 'book_articles', seed['book_articles'])]
    return records, entities, books


def _parse_paper(var_name: str, doc_id: str, paper: object) -> SeededPaper:
    if not doc_id:
        raise SystemExit(f'{var_name} has a paper under an empty doc_id')
    if not isinstance(paper, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} must be a JSON object')
    unknown = set(paper) - _PAPER_FIELDS
    if unknown:
        raise SystemExit(f'{var_name} paper {doc_id!r} has unknown field(s) {sorted(unknown)}')
    title = paper.get('title')
    if not isinstance(title, str) or not title:
        raise SystemExit(f'{var_name} paper {doc_id!r} must set a non-empty "title"')
    markdown = paper.get('markdown')
    readiness = _parse_readiness(var_name, doc_id, paper.get('readiness'))
    if markdown is not None and readiness is not None:
        raise SystemExit(
            f'{var_name} paper {doc_id!r} seeds both a "markdown" rendering and a terminal "readiness": '
            'a paper with a rendering is READY'
        )
    if paper.get('pdf_locations') and paper.get('pdf') is None:
        raise SystemExit(
            f'{var_name} paper {doc_id!r} seeds "pdf_locations" without a "pdf" rendering: '
            'Validate would answer those quotes located and Locate FAILED_PRECONDITION'
        )
    return SeededPaper(
        title=title,
        external_ids=tuple(
            _parse_external_id(var_name, doc_id, i)
            for i in _paper_list(var_name, doc_id, 'external_ids', paper.get('external_ids', []))
        ),
        files=tuple(
            _parse_file(var_name, doc_id, f) for f in _paper_list(var_name, doc_id, 'files', paper.get('files', []))
        ),
        markdown_gcs_uri=_rendering_uri(var_name, doc_id, 'markdown', markdown),
        markdown_from_xml=_markdown_from_xml(var_name, doc_id, markdown),
        markdown_text=_markdown_text(var_name, doc_id, markdown),
        pdf_gcs_uri=_rendering_uri(var_name, doc_id, 'pdf', paper.get('pdf')),
        provenance=_parse_provenance(var_name, doc_id, paper.get('provenance')),
        readiness=readiness,
        pdf_locations=_parse_pdf_locations(var_name, doc_id, paper.get('pdf_locations', {})),
    )


def _parse_provenance(var_name: str, doc_id: str, provenance: object) -> literature_pb2.TextProvenance:
    if provenance is None:
        return literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS
    if not isinstance(provenance, str) or provenance not in _PROVENANCES:
        raise SystemExit(f'{var_name} paper {doc_id!r} "provenance" must be one of {sorted(_PROVENANCES)}')
    return _PROVENANCES[provenance]


def _parse_readiness(var_name: str, doc_id: str, readiness: object) -> literature_pb2.FullTextState | None:
    if readiness is None:
        return None
    if not isinstance(readiness, str) or readiness not in _TERMINAL_READINESS:
        raise SystemExit(f'{var_name} paper {doc_id!r} "readiness" must be one of {sorted(_TERMINAL_READINESS)}')
    return _TERMINAL_READINESS[readiness]


def _parse_external_id(var_name: str, doc_id: str, value: object) -> str:
    """One seeded crosswalk key, under a scheme a request can name — or ``SystemExit``.

    An id under any other scheme, `PMID:123` included, indexes a paper no request reaches: the
    servicer refuses the spelling as unqualified, and the qualified spelling misses the seeded row.
    The key is stored as a lookup resolves it (`external_ids.lookup_key`), so a padded pmid seed is
    not an unreachable one.
    """
    if not isinstance(value, str) or not external_ids_mod.is_qualified(value):
        raise SystemExit(
            f'{var_name} paper {doc_id!r} external id must be '
            f'{"/".join(sorted(external_ids_mod.SCHEMES))}:<value>, got {value!r}'
        )
    try:
        return external_ids_mod.lookup_key(value)
    except ValueError as e:
        raise SystemExit(f'{var_name} paper {doc_id!r} external id {value!r}: {e}') from e


def _paper_list(var_name: str, doc_id: str, key: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise SystemExit(f'{var_name} paper {doc_id!r} field {key!r} must be a JSON array')
    return value


def _parse_file(var_name: str, doc_id: str, f: object) -> SeededFile:
    if not isinstance(f, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} file must be a JSON object')
    role_name = f.get('role')
    if role_name not in _FILE_ROLES:
        raise SystemExit(f'{var_name} paper {doc_id!r} file "role" must be one of {sorted(_FILE_ROLES)}')
    for field in ('name', 'media_type', 'gcs_uri'):
        if not isinstance(f.get(field), str) or not f[field]:
            raise SystemExit(f'{var_name} paper {doc_id!r} file must set a non-empty {field!r}')
    return SeededFile(name=f['name'], role=_FILE_ROLES[role_name], media_type=f['media_type'], gcs_uri=f['gcs_uri'])


def _rendering_uri(var_name: str, doc_id: str, key: str, rendering: object) -> str | None:
    if rendering is None:
        return None
    if not isinstance(rendering, dict) or not isinstance(rendering.get('gcs_uri'), str):
        raise SystemExit(f'{var_name} paper {doc_id!r} {key!r} must be an object with a "gcs_uri"')
    allowed = {'gcs_uri', 'from_xml', 'text'} if key == 'markdown' else {'gcs_uri'}
    unknown = set(rendering) - allowed
    if unknown:
        raise SystemExit(f'{var_name} paper {doc_id!r} {key!r} has unknown field(s) {sorted(unknown)}')
    return rendering['gcs_uri']


def _markdown_from_xml(var_name: str, doc_id: str, markdown: object) -> bool:
    if markdown is None:
        return False
    if not isinstance(markdown, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} "markdown" must be a JSON object')
    from_xml = markdown.get('from_xml', False)
    if not isinstance(from_xml, bool):
        raise SystemExit(f'{var_name} paper {doc_id!r} markdown "from_xml" must be a boolean')
    return from_xml


def _markdown_text(var_name: str, doc_id: str, markdown: object) -> str | None:
    if markdown is None:
        return None
    if not isinstance(markdown, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} "markdown" must be a JSON object')
    text = markdown.get('text')
    if text is None:
        return None
    if not isinstance(text, str) or not text:
        raise SystemExit(f'{var_name} paper {doc_id!r} markdown "text" must be a non-empty string')
    return text


def _parse_pdf_locations(var_name: str, doc_id: str, locations: object) -> dict[str, SeededPdfLocation]:
    if not isinstance(locations, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} "pdf_locations" must be a JSON object')
    parsed: dict[str, SeededPdfLocation] = {}
    for quote, location in locations.items():
        if not isinstance(location, dict) or not isinstance(location.get('page'), int):
            raise SystemExit(f'{var_name} paper {doc_id!r} pdf_locations[{quote!r}] must set an integer "page"')
        rects_raw = location.get('rects', [])
        if not isinstance(rects_raw, list):
            raise SystemExit(f'{var_name} paper {doc_id!r} pdf_locations[{quote!r}] "rects" must be an array')
        rects: list[tuple[float, float, float, float]] = []
        for rect in rects_raw:
            if not isinstance(rect, list) or len(rect) != 4 or not all(isinstance(n, (int, float)) for n in rect):
                raise SystemExit(f'{var_name} paper {doc_id!r} pdf_locations[{quote!r}] rect must be [x, y, w, h]')
            rects.append((float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])))
        parsed[quote] = SeededPdfLocation(page=location['page'], rects=tuple(rects))
    return parsed


def _index_list(source: str, key: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise SystemExit(f'{source} field {key!r} must be a JSON array')
    return value


def _parse_record(source: str, entry: object) -> europe_pmc.Record:
    if not isinstance(entry, Mapping):
        raise SystemExit(f'{source} "records" entries must be JSON objects, got {type(entry).__name__}')
    unknown = set(entry) - _RECORD_FIELDS
    if unknown:
        raise SystemExit(f'{source} record has unknown field(s) {sorted(unknown)}')
    return europe_pmc.Record(
        pmid=_seed_record_pmid(source, _seed_str(source, entry, 'pmid')),
        title=_seed_str(source, entry, 'title'),
        authors=tuple(_seed_strings(source, entry.get('authors'), 'authors')),
        journal=_seed_str(source, entry, 'journal'),
        year=_seed_str(source, entry, 'year'),
        doi=_seed_str(source, entry, 'doi'),
        abstract=_seed_str(source, entry, 'abstract'),
        pmcid=_seed_str(source, entry, 'pmcid'),
    )


def _parse_entity(source: str, entry: object) -> SeededEntity:
    if not isinstance(entry, Mapping):
        raise SystemExit(f'{source} "entities" entries must be JSON objects, got {type(entry).__name__}')
    unknown = set(entry) - _ENTITY_FIELDS
    if unknown:
        raise SystemExit(f'{source} entity has unknown field(s) {sorted(unknown)}')
    entity_id = _seed_str(source, entry, 'id')
    if not entity_id:
        raise SystemExit(f'{source} "entities" entries must set a non-empty "id"')
    pmids = tuple(_seed_pmid(source, pmid) for pmid in _seed_strings(source, entry.get('pmids'), 'pmids'))
    total = entry.get('total_records', len(pmids))
    if not isinstance(total, int) or isinstance(total, bool) or total < len(pmids):
        raise SystemExit(f'{source} "total_records" must be an integer no smaller than the seeded "pmids"')
    return SeededEntity(
        labels=litvar.EntityLabels(
            id=entity_id,
            rsid=_seed_str(source, entry, 'rsid'),
            caids=tuple(_seed_strings(source, entry.get('caids'), 'caids')),
            genes=tuple(_seed_strings(source, entry.get('genes'), 'genes')),
            change=_seed_str(source, entry, 'change'),
        ),
        pmids=pmids,
        total_records=total,
    )


def _parse_book(source: str, entry: object) -> SeededBook:
    if not isinstance(entry, Mapping):
        raise SystemExit(f'{source} "book_articles" entries must be JSON objects, got {type(entry).__name__}')
    unknown = set(entry) - _BOOK_FIELDS
    if unknown:
        raise SystemExit(f'{source} book has unknown field(s) {sorted(unknown)}')
    nbk = _seed_required_str(source, entry, 'nbk')
    if not _BOOK_ACCESSION.fullmatch(nbk):
        raise SystemExit(f'{source} book "nbk" must be a Bookshelf accession (NBK followed by digits), got {nbk!r}')
    return SeededBook(
        pmid=_seed_pmid(source, _seed_required_str(source, entry, 'pmid')),
        nbk=nbk,
        title=_seed_required_str(source, entry, 'title'),
        book_title=_seed_required_str(source, entry, 'book_title'),
        publisher=_seed_required_str(source, entry, 'publisher'),
        contribution_date=_seed_date(source, entry, 'contribution_date'),
        date_revised=_seed_date(source, entry, 'date_revised'),
        authors=tuple(_seed_strings(source, entry.get('authors'), 'authors')),
        abstract=_seed_str(source, entry, 'abstract'),
    )


def _seed_strings(source: str, raw: object, key: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SystemExit(f'{source} {key!r} must be a JSON list of strings')
    for value in raw:
        if not isinstance(value, str) or not value:
            raise SystemExit(f'{source} {key!r} entries must be non-empty strings')
    return list(raw)


def _seed_pmid(source: str, pmid: str) -> str:
    """A seeded PMID under the key every lookup uses, so a padded seed is not an unreachable one."""
    try:
        return pmids_mod.pmid_key(pmid)
    except ValueError as e:
        raise SystemExit(f'{source}: {e}') from e


def _seed_record_pmid(source: str, pmid: str) -> str:
    """A record's PMID key, or '' — a hit the index carries under no PubMed id (a preprint, say)."""
    return _seed_pmid(source, pmid) if pmid else ''


def _seed_str(source: str, mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key, '')
    if not isinstance(value, str):
        raise SystemExit(f'{source} field {key!r} must be a string, got {type(value).__name__}')
    return value


def _seed_required_str(source: str, mapping: Mapping[str, object], key: str) -> str:
    if key not in mapping:
        raise SystemExit(f'{source} field {key!r} is missing')
    value = _seed_str(source, mapping, key)
    if not value:
        raise SystemExit(f'{source} field {key!r} must be a non-empty string')
    return value


def _seed_date(source: str, mapping: Mapping[str, object], key: str) -> datetime.date:
    value = _seed_required_str(source, mapping, key)
    try:
        return datetime.datetime.strptime(value, '%Y-%m-%d').date()  # noqa: DTZ007 — a date, no time zone to carry
    except ValueError as e:
        raise SystemExit(f'{source} field {key!r} must be an ISO date (YYYY-MM-DD), got {value!r}') from e
