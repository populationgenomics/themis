"""The live backend's forwarding: every port method reaches the half that owns it, arguments intact.

``LiveBackend`` is delegation and nothing else, which is exactly the shape a type checker cannot
fully guard: two same-typed arguments swapped between the port and the half behind it type-check and
answer the wrong question. So each forward is exercised against halves that record what they were
handed and answer with a value naming themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import override

import pytest

from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import discovery as discovery_mod
from themis.services.evidence.literature import litcache as litcache_store
from themis.services.evidence.literature import live as live_mod
from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc, pubmed

# One distinguishable answer per method: the forward has to return this object, not merely something
# of the right type.
_PAPER_INFO = literature_pb2.PaperInfo(doc_id='doc-1', title='from the store')
_MARKDOWN = literature_pb2.GetMarkdownResponse(content=literature_pb2.PaperMarkdown(markdown='from the store'))
_CONTENT = literature_pb2.ContentLocation(gcs_uri='gs://from-the-store/x.md', media_type='text/markdown')
_LOCATED = literature_pb2.LocateResponse(offsets=literature_pb2.TextOffsets(start=3, end=9))
_VALIDATED = literature_pb2.ValidateResponse(ok=True, located_in=[literature_pb2.REPRESENTATION_MARKDOWN])
_RESOLVED_IDS = {'doi:10.1/x': 'doc-1'}
_READINESS = {'doc-1': literature_pb2.FULL_TEXT_STATE_READY}
_HITS = europe_pmc.SearchHits(records=[], total_matched=11)
_FETCHED = pubmed.FetchedArticles(articles=[], book_articles=[], pmids_without_record=['111'])
_VARIANT_CENSUS = variants.VariantCensus(entities=(), total_entities=4)
_GENE_ENTITIES = variants.GeneEntities(entities=(), total_in_gene=5, total_matched=2)

_SELECTOR = literature_backend.FileContent(name='figure1.png')
_REQUESTED = variants.RequestedVariant(gene='GENE1', hgvs_c='', protein_change='', rsid='rs00', caid='', entity_id='')


class _RecordingStore(litcache_store.Store):
    """A store that records the call it was handed and answers with the value naming that method."""

    def __init__(self) -> None:
        # No bucket and no crosswalk: every method that would reach one is overridden below.
        self.calls: list[tuple[object, ...]] = []

    @override
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        self.calls.append(('describe_paper', doc_id))
        return _PAPER_INFO

    @override
    async def get_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        self.calls.append(('get_markdown', doc_id, max_chars))
        return _MARKDOWN

    @override
    async def resolve_content(
        self, doc_id: str, selector: literature_backend.ContentSelector
    ) -> literature_pb2.ContentLocation:
        self.calls.append(('resolve_content', doc_id, selector))
        return _CONTENT

    @override
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        self.calls.append(('locate', doc_id, quote, representation))
        return _LOCATED

    @override
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        self.calls.append(('validate', doc_id, quote))
        return _VALIDATED

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        self.calls.append(('resolve_external_ids', list(external_ids)))
        return _RESOLVED_IDS

    @override
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        self.calls.append(('full_text_readiness', list(doc_ids)))
        return _READINESS

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        self.calls.append(('request_conversions', list(doc_ids)))


class _RecordingIndexes(discovery_mod.Indexes):
    """The index half, recording likewise; its two methods take same-typed arguments in pairs."""

    def __init__(self) -> None:
        # No HTTP client: every method that would issue a request is overridden below.
        self.calls: list[tuple[object, ...]] = []

    @override
    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        self.calls.append(('search_europe_pmc', query, max_results))
        return _HITS

    @override
    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        self.calls.append(('fetch_pubmed_articles', list(pmids)))
        return _FETCHED

    @override
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        self.calls.append(('search_litvar', requested, max_results, max_entities))
        return _VARIANT_CENSUS

    @override
    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        self.calls.append(('list_litvar_entities', gene, contains, max_results))
        return _GENE_ENTITIES


# Every port method, the answer its half gives, and the call that half must see. The two
# transposition-prone pairs carry distinct values — `max_results`/`max_entities` and
# `gene`/`contains` — so a swapped forward fails on the recorded call rather than type-checking
# clean and answering the wrong question.
_Forward = Callable[[live_mod.LiveBackend], Coroutine[None, None, object]]

_FORWARDS: list[tuple[str, _Forward, object, tuple[object, ...]]] = [
    ('describe_paper', lambda b: b.describe_paper('doc-1'), _PAPER_INFO, ('describe_paper', 'doc-1')),
    ('get_markdown', lambda b: b.get_markdown('doc-2', 1000), _MARKDOWN, ('get_markdown', 'doc-2', 1000)),
    (
        'resolve_content',
        lambda b: b.resolve_content('doc-3', _SELECTOR),
        _CONTENT,
        ('resolve_content', 'doc-3', _SELECTOR),
    ),
    (
        'locate',
        lambda b: b.locate('doc-4', 'a quote', literature_pb2.REPRESENTATION_PDF),
        _LOCATED,
        ('locate', 'doc-4', 'a quote', literature_pb2.REPRESENTATION_PDF),
    ),
    ('validate', lambda b: b.validate('doc-5', 'another quote'), _VALIDATED, ('validate', 'doc-5', 'another quote')),
    (
        'resolve_external_ids',
        lambda b: b.resolve_external_ids(['doi:10.1/x', 'pmid:111']),
        _RESOLVED_IDS,
        ('resolve_external_ids', ['doi:10.1/x', 'pmid:111']),
    ),
    (
        'full_text_readiness',
        lambda b: b.full_text_readiness(['doc-1', 'doc-2']),
        _READINESS,
        ('full_text_readiness', ['doc-1', 'doc-2']),
    ),
    (
        'request_conversions',
        lambda b: b.request_conversions(['doc-6', 'doc-7']),
        None,
        ('request_conversions', ['doc-6', 'doc-7']),
    ),
    (
        'search_europe_pmc',
        lambda b: b.search_europe_pmc('GENE1 truncating', 13),
        _HITS,
        ('search_europe_pmc', 'GENE1 truncating', 13),
    ),
    (
        'fetch_pubmed_articles',
        lambda b: b.fetch_pubmed_articles(['111', '222']),
        _FETCHED,
        ('fetch_pubmed_articles', ['111', '222']),
    ),
    (
        'search_litvar',
        lambda b: b.search_litvar(_REQUESTED, max_results=7, max_entities=3),
        _VARIANT_CENSUS,
        ('search_litvar', _REQUESTED, 7, 3),
    ),
    (
        'list_litvar_entities',
        lambda b: b.list_litvar_entities(gene='GENE1', contains='a355', max_results=5),
        _GENE_ENTITIES,
        ('list_litvar_entities', 'GENE1', 'a355', 5),
    ),
]


@pytest.mark.parametrize(
    ('call', 'answer', 'recorded'),
    [pytest.param(call, answer, recorded, id=name) for name, call, answer, recorded in _FORWARDS],
)
def test_every_port_method_forwards_to_the_half_that_owns_it(
    call: _Forward, answer: object, recorded: tuple[object, ...]
) -> None:
    store, indexes = _RecordingStore(), _RecordingIndexes()
    result = asyncio.run(call(live_mod.LiveBackend(store, indexes)))
    assert result is answer
    # Concatenated, so this pins the routing too: one call, to one half, and the other untouched.
    assert store.calls + indexes.calls == [recorded]


def test_the_port_is_covered_whole() -> None:
    # The parametrisation is a list, so a method added to the port could be forwarded untested. The
    # ABC knows its own method set; this fails until the new forward is exercised above.
    assert {name for name, _, _, _ in _FORWARDS} == literature_backend.LiteratureBackend.__abstractmethods__
