"""The seeded backend: what it serves, and the seed parsing that refuses to stand one up half-formed.

A malformed seed fails loud rather than serving a partial corpus — an empty answer from an unseeded
half is indistinguishable from one the store or the index genuinely holds nothing for.
"""

from __future__ import annotations

import asyncio

import pytest
from pubmed_proto import pubmed_pb2

from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import fixture, variants
from themis.services.evidence.literature import servicer as servicer_mod
from themis.services.evidence.upstreams import europe_pmc, litvar

_STORE_SOURCE = "THEMIS_LITERATURE_FIXTURE 'store'"
_INDEX_SOURCE = "THEMIS_LITERATURE_FIXTURE 'discovery'"
_EMPTY_INDEX = {'records': [], 'entities': [], 'book_articles': []}
# A book record's seed, whole; synthetic text under a padded spelling of a GeneReviews-class PMID.
_BOOK = {
    'pmid': 'PMID:0020301288',
    'nbk': 'NBK900001',
    'title': 'A synthetic chapter',
    'book_title': 'A synthetic review series',
    'publisher': 'A university press',
    'authors': ['Doe J', 'Roe R'],
    'contribution_date': '2010-03-23',
    'date_revised': '2024-01-04',
    'abstract': 'A synthetic summary.',
}

_ONE_PAPER = {
    'doc-1': {
        'title': 'A paper',
        'markdown': {'gcs_uri': 'gs://fulltext/doc-1/rendering.md', 'from_xml': True},
        'files': [
            {'name': 'f1.png', 'role': 'FIGURE', 'media_type': 'image/png', 'gcs_uri': 'gs://fulltext/doc-1/f1.png'}
        ],
    }
}


def _store_seeded(seed: object) -> fixture.FixtureBackend:
    return fixture.backend_from_seed(seed, _EMPTY_INDEX, store_source=_STORE_SOURCE, index_source=_INDEX_SOURCE)


def _index_seeded(seed: object) -> fixture.FixtureBackend:
    return fixture.backend_from_seed({}, seed, store_source=_STORE_SOURCE, index_source=_INDEX_SOURCE)


# --- The store half's seed ------------------------------------------------------------------------


def test_seed_must_be_an_object_of_papers() -> None:
    with pytest.raises(SystemExit, match='must be a JSON object'):
        _store_seeded(['doc-1'])


def test_paper_requires_a_title() -> None:
    with pytest.raises(SystemExit, match='title'):
        _store_seeded({'doc-1': {'markdown': {'gcs_uri': 'gs://x'}}})


def test_pdf_location_rects_must_be_quads() -> None:
    with pytest.raises(SystemExit, match=r'must be \[x, y, w, h\]'):
        _store_seeded(
            {
                'doc-1': {
                    'title': 'A',
                    'pdf': {'gcs_uri': 'gs://x'},
                    'pdf_locations': {'q': {'page': 0, 'rects': [[1, 2, 3]]}},
                }
            }
        )


def test_a_rendering_without_text_is_the_store_fault_on_every_read_that_needs_it() -> None:
    # A seeded rendering with no text is the manifest-promises-what-the-store-cannot-produce state;
    # the live store raises it from GetMarkdown, Locate and Validate alike, so the fixture must too —
    # a fixture validating a quote against a text it cannot serve answers from nowhere.
    faulty = _store_seeded({'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://x'}}})
    with pytest.raises(literature_backend.MissingRenderingBlobError):
        asyncio.run(faulty.get_markdown('doc-1', 1000))
    with pytest.raises(literature_backend.MissingRenderingBlobError):
        asyncio.run(faulty.locate('doc-1', 'a quote', literature_pb2.REPRESENTATION_MARKDOWN))
    with pytest.raises(literature_backend.MissingRenderingBlobError):
        asyncio.run(faulty.validate('doc-1', 'a quote'))


def test_locate_and_validate_answer_from_the_text_getmarkdown_serves() -> None:
    # One copy of the text answers all three, through the live matcher, so the offline quote
    # answers can never contradict the markdown a consumer just read.
    text = '# A paper\n\nThe assay showed reduced channel activity in vitro.\n'
    seeded = _store_seeded({'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://x', 'text': text}}})
    located = asyncio.run(seeded.locate('doc-1', 'reduced channel activity', literature_pb2.REPRESENTATION_MARKDOWN))
    start, end = located.offsets.start, located.offsets.end
    assert text[start:end] == 'reduced channel activity'
    assert asyncio.run(seeded.validate('doc-1', 'reduced channel activity')).ok
    assert not asyncio.run(seeded.validate('doc-1', 'never written')).ok


def test_pdf_locations_need_the_pdf_they_locate_in() -> None:
    # Validate reads the location map and answers the quote located; Locate reads the rendering and
    # answers FAILED_PRECONDITION for the same quote. A seed cannot state both.
    with pytest.raises(SystemExit, match='pdf'):
        _store_seeded({'doc-1': {'title': 'A', 'pdf_locations': {'q': {'page': 0}}}})


@pytest.mark.parametrize(
    'external_id',
    [
        pytest.param('PMID:123', id='upper-case-scheme'),
        pytest.param('pii:S0140-6736', id='scheme-no-request-names'),
        pytest.param('pmid:', id='no-value'),
        pytest.param('123', id='unqualified'),
    ],
)
def test_an_external_id_no_request_can_name_is_refused(external_id: str) -> None:
    # Indexed under it the paper is unreachable: the servicer refuses the spelling as unqualified,
    # and the qualified spelling misses the row — an offline store that silently holds nothing.
    with pytest.raises(SystemExit, match='external id'):
        _store_seeded({'doc-1': {'title': 'A', 'external_ids': [external_id]}})


def test_file_role_must_be_known() -> None:
    file = {'name': 'f', 'role': 'CHART', 'media_type': 'image/png', 'gcs_uri': 'gs://c/f'}
    with pytest.raises(SystemExit, match='role'):
        _store_seeded({'doc-1': {'title': 'A', 'files': [file]}})


def test_paper_rejects_an_unknown_field() -> None:
    # `pdf_location` (singular) is a plausible typo of `pdf_locations`; dropping the field silently
    # would serve the paper with no quote locations at all.
    with pytest.raises(SystemExit, match='unknown field'):
        _store_seeded({'doc-1': {'title': 'A', 'pdf_location': {}}})


def test_markdown_object_rejects_an_unknown_field() -> None:
    # `fromXml` (camelCase) is a plausible typo of `from_xml` inside the nested markdown object.
    with pytest.raises(SystemExit, match='unknown field'):
        _store_seeded({'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://x', 'fromXml': True}}})


def test_an_empty_store_is_an_explicit_valid_seed() -> None:
    backend = _store_seeded({})
    with pytest.raises(literature_backend.UnknownPaperError):
        asyncio.run(backend.describe_paper('doc-1'))


def test_a_valid_seed_builds_a_describable_paper() -> None:
    info = asyncio.run(_store_seeded(_ONE_PAPER).describe_paper('doc-1'))
    assert info.title == 'A paper'
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN
    assert [f.name for f in info.files] == ['f1.png']


def test_readiness_follows_what_the_paper_holds() -> None:
    # The same derivation `litcache.outcome` makes from the layout: a rendering is text to serve, a
    # terminal seed is the stop condition, and anything else has not settled.
    seed = {
        'ready': {'title': 'A', 'markdown': {'gcs_uri': 'gs://c/r.md', 'text': '# A'}},
        'pending': {'title': 'B', 'pdf': {'gcs_uri': 'gs://c/b.pdf'}},
        'failed': {'title': 'C', 'pdf': {'gcs_uri': 'gs://c/c.pdf'}, 'readiness': 'FAILED'},
        'nothing': {'title': 'D', 'readiness': 'NO_FULL_TEXT'},
    }
    states = asyncio.run(_store_seeded(seed).full_text_readiness(['ready', 'pending', 'failed', 'nothing', 'absent']))
    assert states == {
        'ready': literature_pb2.FULL_TEXT_STATE_READY,
        'pending': literature_pb2.FULL_TEXT_STATE_PENDING,
        'failed': literature_pb2.FULL_TEXT_STATE_FAILED,
        'nothing': literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT,
        'absent': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    }


def test_a_seeded_paper_serves_the_text_its_seed_gives_it() -> None:
    text = '# A paper\n\nThe channel showed markedly reduced ATP sensitivity.\n'
    seed = {'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://c/r.md', 'text': text}}}
    result = asyncio.run(_store_seeded(seed).get_markdown('doc-1', servicer_mod._DEFAULT_MAX_CHARS))
    assert result.WhichOneof('result') == 'content'
    assert result.content.markdown == text
    assert result.content.provenance == literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS


def test_a_seeded_paper_is_served_under_the_same_read_budget_as_the_store() -> None:
    # The budget is the reader's bound on what it may quote, so an offline run has to meet the same
    # one: a fixture serving whole text would let a capture anchor quotes the deployed read cuts.
    budget = servicer_mod._DEFAULT_MAX_CHARS
    line = 'The channel showed markedly reduced ATP sensitivity in vitro.\n'
    oversized = line * (1 + budget // len(line))
    seed = {'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://c/r.md', 'text': oversized}}}
    result = asyncio.run(_store_seeded(seed).get_markdown('doc-1', budget))
    assert result.content.total_chars == len(oversized) > budget  # the cut, and how much lies past it
    kept, marker = result.content.markdown.split('\n\n---\n\n', 1)
    assert len(kept) <= budget
    assert kept.endswith('in vitro.')  # the cut lands between lines, never mid-sentence
    assert 'cannot be quoted or cited' in marker


def test_provenance_is_seedable_and_reaches_the_text_it_qualifies() -> None:
    seed = {'doc-1': {'title': 'A', 'provenance': 'SUPPLIED', 'markdown': {'gcs_uri': 'gs://c/r.md', 'text': '# A'}}}
    result = asyncio.run(_store_seeded(seed).get_markdown('doc-1', servicer_mod._DEFAULT_MAX_CHARS))
    assert result.content.provenance == literature_pb2.TEXT_PROVENANCE_SUPPLIED


def test_a_paper_without_a_rendering_reports_why_rather_than_empty_text() -> None:
    seed = {'doc-1': {'title': 'A', 'readiness': 'NO_FULL_TEXT'}}
    result = asyncio.run(_store_seeded(seed).get_markdown('doc-1', servicer_mod._DEFAULT_MAX_CHARS))
    assert result.WhichOneof('result') == 'unavailable'
    assert result.unavailable.state == literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT


def test_a_rendering_the_seed_gives_no_text_for_is_a_store_fault() -> None:
    # The store's own fault case: a listed rendering whose text cannot be produced. Serving an
    # empty markdown instead would read as a paper that says nothing.
    seed = {'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://c/r.md'}}}
    with pytest.raises(literature_backend.MissingRenderingBlobError):
        asyncio.run(_store_seeded(seed).get_markdown('doc-1', servicer_mod._DEFAULT_MAX_CHARS))


def test_seeding_both_a_rendering_and_a_terminal_readiness_is_rejected() -> None:
    with pytest.raises(SystemExit, match='READY'):
        _store_seeded({'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://c/r.md'}, 'readiness': 'FAILED'}})


def test_a_derivable_readiness_is_not_seedable() -> None:
    # READY, PENDING and UNKNOWN_PAPER follow from what the store holds; a seed asserting one would
    # let the fixture contradict the derivation every other reader trusts.
    with pytest.raises(SystemExit, match='readiness'):
        _store_seeded({'doc-1': {'title': 'A', 'readiness': 'READY'}})


def test_two_papers_cannot_claim_one_external_id() -> None:
    # The crosswalk's primary key makes this impossible upstream; a seed that states it would have
    # one of the two papers silently unreachable by its own identifier.
    with pytest.raises(SystemExit, match='claimed by both'):
        _store_seeded(
            {
                'doc-1': {'title': 'A', 'external_ids': ['doi:10.1/X']},
                'doc-2': {'title': 'B', 'external_ids': ['doi:10.1/x']},
            }
        )


def test_a_paper_under_an_empty_doc_id_is_rejected() -> None:
    # Every answer carries the doc_id back, and an empty one is the wire's "no paper here".
    with pytest.raises(SystemExit, match='empty doc_id'):
        _store_seeded({'': {'title': 'A'}})


def test_an_unknown_provenance_is_rejected() -> None:
    with pytest.raises(SystemExit, match='provenance'):
        _store_seeded({'doc-1': {'title': 'A', 'provenance': 'PAYWALLED'}})


def test_markdown_text_must_be_a_non_empty_string() -> None:
    with pytest.raises(SystemExit, match='text'):
        _store_seeded({'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://c/r.md', 'text': 42}}})


# --- The index half -------------------------------------------------------------------------------


def _requested(**fields: str) -> variants.RequestedVariant:
    return variants.RequestedVariant(
        **{'gene': '', 'hgvs_c': '', 'protein_change': '', 'rsid': '', 'caid': '', 'entity_id': '', **fields}
    )


def _entity(
    entity_id: str,
    *,
    rsid: str = '',
    caids: tuple[str, ...] = (),
    genes: tuple[str, ...] = ('GENE1',),
    change: str = '',
    pmids: tuple[str, ...] = (),
    total: int | None = None,
) -> fixture.SeededEntity:
    return fixture.SeededEntity(
        labels=litvar.EntityLabels(id=entity_id, rsid=rsid, caids=caids, genes=genes, change=change),
        pmids=pmids,
        total_records=len(pmids) if total is None else total,
    )


def _record(pmid: str, **fields: str) -> europe_pmc.Record:
    return europe_pmc.Record(
        **{
            'pmid': pmid,
            'title': f'Paper {pmid}',
            'authors': (),
            'journal': '',
            'year': '',
            'doi': '',
            'abstract': 'A.',
            'pmcid': '',
            **fields,
        }  # pyright: ignore[reportArgumentType]
    )


_RECORDS = (
    _record('111', title='GENE1 truncating variant', journal='J Med Genet', year='2021', doi='10.1/x'),
    _record('222', title='GENE1 missense review', journal='Hum Mutat', year='2019', doi='10.2/y'),
    _record('333', title='GENE1 splicing assay under the earlier numbering', journal='J Lipid Res', year='2009'),
    # Indexed, and states no abstract — the shape a letter or a comment has upstream.
    _record('444', title='Comment on a sample-size calculation', abstract=''),
)

# The five entity shapes one variant is split across upstream, seeded so a test can reach each: the
# position-scoped rsID entity spanning two alleles, the allele-scoped child under one of them, the
# change-keyed entity under the current numbering, the one under an earlier numbering that carries no
# key at all, and an entity the index labels with another gene.
_ENTITIES = (
    _entity(
        'litvar@rs00##', rsid='rs00', caids=('CA1000', 'CA2000'), change='c.1063G>A', pmids=('111', '222'), total=5
    ),
    _entity('litvar@CA1000#rs00##', rsid='rs00', caids=('CA1000',), change='c.1063G>A', pmids=('111',)),
    _entity('litvar@#77#p.A355T', change='p.A355T', pmids=('222',)),
    _entity('litvar@#77#p.A340T', change='p.A340T', pmids=('333',)),
    _entity('litvar@rs99##', rsid='rs99', genes=('GENE2',), pmids=('333',)),
)


def _indexed() -> fixture.FixtureBackend:
    return fixture.FixtureBackend({}, _RECORDS, _ENTITIES)


def test_search_matches_the_seeded_records_and_clamps_to_the_budget() -> None:
    whole = asyncio.run(_indexed().search_europe_pmc('GENE1', 10))
    assert [record.pmid for record in whole.records] == ['111', '222', '333']
    assert whole.total_matched == 3
    assert len(asyncio.run(_indexed().search_europe_pmc('', 10)).records) == len(_RECORDS)


def test_search_counts_every_match_the_budget_cut() -> None:
    # The census is what makes a clamped page legible as a prefix rather than as the whole match.
    cut = asyncio.run(_indexed().search_europe_pmc('GENE1', 1))
    assert [record.pmid for record in cut.records] == ['111']
    assert cut.total_matched == 3


def test_fetch_pubmed_articles_lands_every_requested_pmid_in_exactly_one_outcome() -> None:
    fetched = asyncio.run(_indexed().fetch_pubmed_articles(['222', '111', '999']))
    assert [article.medline_citation.pmid.value for article in fetched.articles] == ['222', '111']
    assert fetched.pmids_without_record == ['999']


def test_fetch_pubmed_articles_serves_a_record_with_no_abstract_whole() -> None:
    fetched = asyncio.run(_indexed().fetch_pubmed_articles(['444', '999']))
    (no_abstract,) = fetched.articles
    assert not no_abstract.medline_citation.article.HasField('abstract')
    # The bibliography a citation needs outlives the missing abstract.
    assert no_abstract.medline_citation.article.article_title.value
    assert fetched.pmids_without_record == ['999']


def _search_litvar(*, max_results: int = 10, max_entities: int = 8, **fields: str) -> variants.VariantCensus:
    return asyncio.run(
        _indexed().search_litvar(_requested(**fields), max_results=max_results, max_entities=max_entities)
    )


def test_every_entity_an_identifier_reaches_is_returned() -> None:
    # One rsID keys both the position-scoped entity and the allele-scoped one beneath it. Answering
    # with either alone is a choice the service has no grounds to make: the wide one mixes alleles,
    # the narrow one drops papers the wide one has.
    found = _search_litvar(gene='GENE1', rsid='rs00')
    assert [entity.labels.id for entity in found.entities] == ['litvar@rs00##', 'litvar@CA1000#rs00##']
    assert list(found.entities[0].pmids) == ['111', '222']  # index score order


def test_a_disagreeing_label_is_stated_rather_than_dropped() -> None:
    # The identifier resolves; the index labels the entity with another gene. The service cannot tell
    # a wrong rsID from a mislabelled entity, so it reports and returns rather than raising — raising
    # would discard the papers of whichever of the two is right.
    entity = _search_litvar(gene='GENE1', rsid='rs99').entities[0]
    assert entity.agreement.gene is variants.Agreement.DIFFERS
    assert entity.agreement.rsid is variants.Agreement.AGREES
    assert list(entity.pmids) == ['333']


def test_a_caid_matches_however_it_is_padded() -> None:
    # The zero-padded and unpadded spellings are one ClinGen id; sources disagree on which to write.
    padded = _search_litvar(gene='GENE1', caid='CA0001000')
    unpadded = _search_litvar(gene='GENE1', caid='CA1000')
    assert [e.labels.id for e in padded.entities] == [e.labels.id for e in unpadded.entities]
    assert all(entity.agreement.caid is variants.Agreement.AGREES for entity in padded.entities)


def test_a_protein_change_matches_across_residue_spellings() -> None:
    three_letter = _search_litvar(gene='GENE1', protein_change='NP_1.1:p.Ala355Thr')
    one_letter = _search_litvar(gene='GENE1', protein_change='p.A355T')
    assert [e.labels.id for e in three_letter.entities] == [e.labels.id for e in one_letter.entities]
    assert three_letter.entities[0].agreement.change is variants.Agreement.AGREES


def test_an_entity_under_an_earlier_numbering_is_reached_only_by_its_id() -> None:
    # The entity under the earlier numbering carries no rsID and no ClinGen id, so nothing the caller
    # can put in a variant request reaches it. Its id, from the gene listing, does.
    by_identifiers = _search_litvar(gene='GENE1', rsid='rs00', hgvs_c='c.1063G>A')
    assert all(entity.labels.id != 'litvar@#77#p.A340T' for entity in by_identifiers.entities)

    by_id = _search_litvar(entity_id='litvar@#77#p.A340T')
    assert [entity.labels.id for entity in by_id.entities] == ['litvar@#77#p.A340T']
    assert list(by_id.entities[0].pmids) == ['333']


def test_an_earlier_numbering_reads_as_a_difference_not_an_absence() -> None:
    # The entity indexes the variant's earlier literature under the numbering in force then. Saying
    # so is the whole of what the service can do; offsetting the residue is the caller's reading.
    entity = _search_litvar(entity_id='litvar@#77#p.A340T', gene='GENE1', protein_change='p.A355T').entities[0]
    assert entity.agreement.change is variants.Agreement.DIFFERS
    assert entity.agreement.gene is variants.Agreement.AGREES


def test_nothing_resolving_is_an_empty_answer_not_a_failure() -> None:
    found = _search_litvar(gene='GENE1', rsid='rs999999')
    assert found.entities == ()
    assert found.total_entities == 0


def test_the_pmid_budget_is_per_entity() -> None:
    # Each entity's list stops at the budget on its own; neither is anything but its own top-ranked
    # prefix, and one entity linking many records cannot starve another.
    found = _search_litvar(gene='GENE1', rsid='rs00', max_results=1)
    assert [list(entity.pmids) for entity in found.entities] == [['111'], ['111']]


def test_the_entity_ceiling_cuts_the_candidate_set_and_says_so() -> None:
    found = _search_litvar(gene='GENE1', rsid='rs00', max_entities=1)
    assert len(found.entities) == 1
    assert found.total_entities == 2  # the cut is stated, so the caller knows candidates went unnamed


def test_a_prefix_seeded_entity_reads_as_a_prefix_however_generous_the_budget() -> None:
    # A total_records above the seeded PMIDs is an index linking records the seed does not list; the
    # per-entity census is what keeps that legible at any budget.
    found = _search_litvar(gene='GENE1', rsid='rs00', max_results=50)
    prefix = found.entities[0]
    assert prefix.total_records > len(prefix.pmids)


def test_a_shared_record_is_reported_under_each_entity_that_holds_it() -> None:
    # The entity sets are not a partition, so a PMID under two entities is under both here; the
    # caller deduplicates before counting.
    listed = [pmid for entity in _search_litvar(gene='GENE1', rsid='rs00').entities for pmid in entity.pmids]
    assert sorted(listed) == ['111', '111', '222']


def test_a_gene_level_entity_is_never_a_variant_lookups_answer() -> None:
    # names_an_allele is the port's contract, not one adapter's: a seeded gene-level entity must
    # answer here exactly as the index's own does.
    backend = fixture.FixtureBackend(
        {}, _RECORDS, [_entity('litvar@#77#', pmids=('111',)), _entity('litvar@rs00##', rsid='rs00', pmids=('111',))]
    )
    found = asyncio.run(backend.search_litvar(_requested(rsid='rs00'), max_results=10, max_entities=8))
    assert [entity.labels.id for entity in found.entities] == ['litvar@rs00##']


def test_the_gene_inventory_lists_the_seeded_entities_of_one_gene() -> None:
    listed = asyncio.run(_indexed().list_litvar_entities(gene='GENE1', contains='', max_results=50))
    assert 'litvar@rs99##' not in [entity.id for entity in listed.entities]  # GENE2's
    assert listed.total_in_gene == listed.total_matched == len(listed.entities)
    counts = [entity.total_records for entity in listed.entities]
    assert counts == sorted(counts, reverse=True)  # most-published first


def test_the_gene_inventory_narrows_on_the_id_and_states_what_it_dropped() -> None:
    listed = asyncio.run(_indexed().list_litvar_entities(gene='GENE1', contains='a340', max_results=50))
    assert [entity.id for entity in listed.entities] == ['litvar@#77#p.A340T']
    assert listed.total_matched == 1
    assert listed.total_in_gene > listed.total_matched


def test_a_seeded_entity_listing_a_pmid_the_records_lack_is_refused() -> None:
    # A dropped PMID would shorten a variant lookup's list without the census saying so, which is
    # the one failure the seeded index must not be able to stage.
    with pytest.raises(ValueError, match='lists PMIDs no seeded record or book carries'):
        fixture.FixtureBackend({}, [], [_entity('litvar@rs00##', rsid='rs00', pmids=('999',))])


def test_two_records_under_one_pmid_are_refused() -> None:
    # One identifier names one record; a second under the same PMID would displace the first
    # silently, and the entity that lists it would then answer with the wrong paper.
    with pytest.raises(ValueError, match='seeded twice'):
        fixture.FixtureBackend({}, [_record('111'), _record('111', title='Another')], [])


# --- The index half's seed ------------------------------------------------------------------------


def test_an_explicitly_empty_index_is_a_valid_seed() -> None:
    empty = _index_seeded({'records': [], 'entities': [], 'book_articles': []})
    assert asyncio.run(empty.fetch_pubmed_articles(['111'])).pmids_without_record == ['111']


@pytest.mark.parametrize(
    'seed',
    [{'records': []}, {'entities': []}, {'records': [], 'entities': []}],
    ids=['entities-absent', 'records-absent', 'book-articles-absent'],
)
def test_an_index_seed_missing_a_list_exits(seed: object) -> None:
    # Defaulting the absent list to [] would read a forgotten key as "the index holds nothing" —
    # the silently empty index an offline run has no way to tell from a seeded one.
    with pytest.raises(SystemExit, match='missing field'):
        _index_seeded(seed)


def test_the_seeded_index_is_keyed_the_way_lookups_key_a_pmid() -> None:
    # A seed spelled otherwise than the lookup keys it would be seeded and unreachable — an index
    # that silently holds nothing, which is exactly what an offline run cannot detect.
    seeded = _index_seeded(
        {
            'records': [{'pmid': 'PMID:0000111', 'title': 'A paper', 'abstract': 'Something.'}],
            'entities': [],
            'book_articles': [],
        }
    )
    fetched = asyncio.run(seeded.fetch_pubmed_articles(['111']))
    assert [article.medline_citation.pmid.value for article in fetched.articles] == ['111']


def test_a_seeded_record_carries_its_authors_one_per_entry() -> None:
    # The index states a list, and the wire carries a list: a seed that could only state one joined
    # string would be the one place a byline arrives in a shape no live answer has.
    seeded = _index_seeded(
        {
            'records': [{'pmid': '111', 'title': 'A paper', 'authors': ['Xu W', 'Yang X']}],
            'entities': [],
            'book_articles': [],
        }
    )
    (article,) = asyncio.run(seeded.fetch_pubmed_articles(['111'])).articles
    assert [author.last_name for author in article.medline_citation.article.author_list.author] == ['Xu W', 'Yang X']


@pytest.mark.parametrize('pmid', ['PMC4072343', '12a'])
def test_the_seeded_index_refuses_a_pmid_that_is_not_one(pmid: str) -> None:
    with pytest.raises(SystemExit):
        _index_seeded({'records': [{'pmid': pmid, 'title': 'A paper'}], 'entities': [], 'book_articles': []})


def test_a_seeded_book_is_answered_whole_as_a_kind_of_record_never_absence() -> None:
    # The offline index has to be able to put a caller in front of the third outcome — a GeneReviews
    # chapter answered as "nothing indexed" is the misreport the arm exists to prevent — and in front
    # of the record whole, in the shape the live index answers it.
    seeded = _index_seeded({'records': [], 'entities': [], 'book_articles': [_BOOK]})
    fetched = asyncio.run(seeded.fetch_pubmed_articles(['20301288', '999']))
    (book,) = fetched.book_articles
    document = book.book_document
    assert document.pmid.value == '20301288'  # seeded under the key every lookup uses
    assert [(i.id_type, i.value) for i in document.article_id_list] == [
        (pubmed_pb2.ArticleId.ID_TYPE_BOOKACCESSION, 'NBK900001')
    ]
    assert document.article_title.value == 'A synthetic chapter'
    assert document.book.book_title.value == 'A synthetic review series'
    assert document.book.publisher.publisher_name == 'A university press'
    assert [author.last_name for author in document.author_list[0].author] == ['Doe J', 'Roe R']
    assert document.abstract.abstract_text[0].value == 'A synthetic summary.'
    assert document.contribution_date.ToDatetime().date().isoformat() == '2010-03-23'
    assert document.date_revised.ToDatetime().date().isoformat() == '2024-01-04'
    assert fetched.pmids_without_record == ['999']


def test_a_pmid_cannot_be_both_a_record_and_a_book() -> None:
    with pytest.raises(SystemExit, match='both'):
        _index_seeded({'records': [{'pmid': '20301288', 'title': 'A'}], 'entities': [], 'book_articles': [_BOOK]})


def test_a_pmid_cannot_be_seeded_as_two_books() -> None:
    with pytest.raises(SystemExit, match='two books'):
        _index_seeded({'records': [], 'entities': [], 'book_articles': [_BOOK, {**_BOOK, 'nbk': 'NBK900002'}]})


def test_an_entity_may_list_a_book_pmid() -> None:
    # LitVar links GeneReviews chapters like any publication; a census naming one is not a seed
    # naming a record that does not exist.
    seeded = _index_seeded(
        {
            'records': [],
            'entities': [{'id': 'litvar@rs00##', 'rsid': 'rs00', 'pmids': ['20301288']}],
            'book_articles': [_BOOK],
        }
    )
    found = asyncio.run(seeded.search_litvar(_requested(rsid='rs00'), max_results=10, max_entities=8))
    assert list(found.entities[0].pmids) == ['20301288']


def test_a_record_under_no_pubmed_id_is_searchable_and_never_fetched_by_one() -> None:
    # A preprint or a PMC-only deposit is a hit the index carries under no PubMed id; the seed can
    # say so, two of them collide on nothing, and no PMID reaches either.
    seeded = _index_seeded(
        {
            'records': [
                {'pmid': '', 'title': 'A preprint', 'doi': '10.1101/1'},
                {'title': 'Another preprint', 'doi': '10.1101/2'},
            ],
            'entities': [],
            'book_articles': [],
        }
    )
    hits = asyncio.run(seeded.search_europe_pmc('preprint', 10))
    assert [record.pmid for record in hits.records] == ['', '']
    assert asyncio.run(seeded.fetch_pubmed_articles(['111'])).pmids_without_record == ['111']


def test_a_seeded_entity_carries_its_labels_and_its_ranked_pmids() -> None:
    seeded = _index_seeded(
        {
            'records': [{'pmid': '111', 'title': 'A paper'}, {'pmid': '222', 'title': 'Another'}],
            'entities': [
                {
                    'id': 'litvar@rs00##',
                    'rsid': 'rs00',
                    'caids': ['CA1000'],
                    'genes': ['GENE1'],
                    'change': 'c.1063G>A',
                    'pmids': ['111', '222'],
                    'total_records': 5,
                }
            ],
            'book_articles': [],
        }
    )
    found = asyncio.run(seeded.search_litvar(_requested(rsid='rs00'), max_results=10, max_entities=8))
    (entity,) = found.entities
    assert entity.labels.caids == ('CA1000',)
    assert entity.total_records == 5  # above the seeded list: the returned list is a prefix
    assert list(entity.pmids) == ['111', '222']


def test_a_count_below_the_seeded_pmids_is_refused() -> None:
    # A total under the list it accompanies makes the truncation census say the returned list is
    # longer than the whole, which is not a state the real index can be in.
    with pytest.raises(SystemExit, match='total_records'):
        _index_seeded(
            {
                'records': [{'pmid': '1'}, {'pmid': '2'}],
                'entities': [{'id': 'e', 'pmids': ['1', '2'], 'total_records': 1}],
                'book_articles': [],
            }
        )


def test_a_seeded_entity_listing_an_unseeded_pmid_exits() -> None:
    with pytest.raises(SystemExit, match='lists PMIDs no seeded record or book carries'):
        _index_seeded(
            {
                'records': [],
                'entities': [{'id': 'litvar@rs00##', 'rsid': 'rs00', 'pmids': ['999']}],
                'book_articles': [],
            }
        )


@pytest.mark.parametrize(
    'seed',
    [
        pytest.param({'records': [], 'entities': [{'pmids': ['1']}], 'book_articles': []}, id='entity-without-an-id'),
        pytest.param(
            {'records': [], 'entities': [{'id': 'e', 'pmids': [7]}], 'book_articles': []}, id='non-string-pmid'
        ),
        pytest.param(
            {'records': [], 'entities': [{'id': 'e', 'genes': 'GENE1'}], 'book_articles': []}, id='genes-not-a-list'
        ),
        pytest.param({'records': [], 'entities': 'litvar@rs00##', 'book_articles': []}, id='entities-not-a-list'),
        pytest.param(
            {'records': [{'pmid': '1', 'title': 7}], 'entities': [], 'book_articles': []}, id='non-string-title'
        ),
        pytest.param(
            {'records': [{'pmid': '1', 'authors': 'Xu W'}], 'entities': [], 'book_articles': []},
            id='authors-not-a-list',
        ),
        pytest.param({'records': ['111'], 'entities': [], 'book_articles': []}, id='record-not-an-object'),
        pytest.param(
            {'records': [{'pmid': '1', 'open_access': {}}], 'entities': [], 'book_articles': []},
            id='unknown-record-field',
        ),
        pytest.param(
            {'records': [], 'entities': [{'id': 'e', 'records': []}], 'book_articles': []}, id='unknown-entity-field'
        ),
        pytest.param({'records': [], 'entities': [], 'book_articles': ['20301288']}, id='book-not-an-object'),
        pytest.param(
            {'records': [], 'entities': [], 'book_articles': [{k: v for k, v in _BOOK.items() if k != 'nbk'}]},
            id='book-without-its-accession',
        ),
        pytest.param(
            {'records': [], 'entities': [], 'book_articles': [{**_BOOK, 'publisher': ''}]},
            id='book-without-a-publisher',
        ),
        pytest.param(
            {'records': [], 'entities': [], 'book_articles': [{**_BOOK, 'nbk': '900001'}]}, id='book-accession-not-nbk'
        ),
        pytest.param(
            {'records': [], 'entities': [], 'book_articles': [{**_BOOK, 'date_revised': '04/01/2024'}]},
            id='book-date-not-iso',
        ),
        pytest.param(
            {'records': [], 'entities': [], 'book_articles': [{**_BOOK, 'authors': 'Doe J'}]},
            id='book-authors-not-a-list',
        ),
        pytest.param(
            {'records': [], 'entities': [], 'book_articles': [{**_BOOK, 'edition': '2'}]}, id='unknown-book-field'
        ),
        pytest.param({'papers': []}, id='unknown-top-level-field'),
        pytest.param([], id='not-an-object'),
    ],
)
def test_a_malformed_seed_exits(seed: object) -> None:
    # An unknown field is rejected rather than dropped: a typo'd key would otherwise seed a record
    # missing exactly the data the test or deploy meant to give it.
    with pytest.raises(SystemExit):
        _index_seeded(seed)


_DUPLICATE_EXTERNAL_ID = {
    'doc-1': {'title': 'A', 'external_ids': ['doi:10.1/X']},
    'doc-2': {'title': 'B', 'external_ids': ['doi:10.1/x']},
}
_ENTITY_WITHOUT_ITS_RECORD = {
    'records': [],
    'entities': [{'id': 'litvar@rs00##', 'pmids': ['999']}],
    'book_articles': [],
}


def test_a_cross_referential_failure_names_the_section_that_holds_the_value_to_fix() -> None:
    # These two are caught when the backend is built, once both halves are in hand, so nothing about
    # where they came from is left in the exception itself. An operator reading a startup failure
    # still has to be sent to the section holding the value to fix, not to the document.
    with pytest.raises(SystemExit, match=_STORE_SOURCE):
        _store_seeded(_DUPLICATE_EXTERNAL_ID)
    with pytest.raises(SystemExit, match=_INDEX_SOURCE):
        _index_seeded(_ENTITY_WITHOUT_ITS_RECORD)


def test_a_document_broken_in_both_sections_names_the_first_one() -> None:
    # An operator fixes a seed top to bottom, so the first failure they are shown has to be the
    # first one there is to fix. Reporting the later section would send them past a broken store to
    # an index whose own error they would then fix first.
    with pytest.raises(SystemExit, match=_STORE_SOURCE):
        fixture.backend_from_seed(
            _DUPLICATE_EXTERNAL_ID,
            _ENTITY_WITHOUT_ITS_RECORD,
            store_source=_STORE_SOURCE,
            index_source=_INDEX_SOURCE,
        )
