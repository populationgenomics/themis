"""NCBI ClinVar adapter: the archive one accession names, submission detail, and the gene-pool census.

The E-utilities calls are driven by an httpx2 `MockTransport` over committed payloads (accession
VCV001731988, gene `NF1` capped at 5 of 5866); the rate-limit delay is patched to zero so no test
sleeps or hits the network.

Observation parsing is exercised over two further real archives, trimmed to the assertions that carry
each shape: HFE c.845G>A (a cohort observed at two zygosities, a literature submitter's per-family
notes and citations, a registry submission with an age and no classification) and PAH c.1315+1G>A (a
ClinGen expert-panel curation with its evidence-repository link and stated inheritance).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable

import clinvar_proto
import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import clinvar

_FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'
_FIXTURE = json.loads((_FIXTURES / 'clinvar.json').read_bytes())
_VCV_XML = (_FIXTURES / 'clinvar_vcv.xml').read_bytes()
# What efetch answered VCV999999999 with, recorded off the live endpoint.
_NO_RECORD_ENVELOPE = (_FIXTURES / 'clinvar_efetch_no_record.xml').read_bytes()
_GENE = _FIXTURE['_gene']
_VCV = 'VCV001731988'
# The bound the pool is fetched under here; the rpc takes it from the caller and defaults nothing.
_POOL_LIMIT = 500


def _archive(fixture: str) -> clinvar.ClinvarArchive:
    """What `fetch_variant_archive` returns for a committed archive, fetched end to end."""
    content = (_FIXTURES / fixture).read_bytes()
    return _run(
        lambda _r: httpx2.Response(200, content=content), lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c)
    )


def _record(fixture: str) -> clinvar.ClinvarRecordData:
    return _archive(fixture).record


def _observations(record: clinvar.ClinvarRecordData) -> list[clinvar.ClinvarObservationData]:
    return [o for s in record.submissions for o in s.observations]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clinvar, '_RATE_LIMIT_DELAY_S', 0)


def _handler(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    if path.endswith('/esearch.fcgi'):
        term = request.url.params['term']
        return httpx2.Response(200, json=_FIXTURE['esearch_gene' if '[gene]' in term else 'esearch_variant'])
    if path.endswith('/efetch.fcgi'):
        return httpx2.Response(200, content=_VCV_XML)
    if path.endswith('/esummary.fcgi'):
        return httpx2.Response(200, json=_FIXTURE['esummary_gene'])
    raise AssertionError(f'unexpected request path {path!r}')


def _run[T](
    handler: Callable[[httpx2.Request], httpx2.Response], call: Callable[[httpx2.AsyncClient], Awaitable[T]]
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _pool(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    gene: str = _GENE,
    floor: int = 1,
    limit: int = _POOL_LIMIT,
) -> clinvar.ClinvarGenePool:
    """The gene pool over ``handler``; the floor and the bound are stated because the rpc states them."""

    async def call(client: httpx2.AsyncClient) -> clinvar.ClinvarGenePool:
        return await clinvar.fetch_gene_pool(gene, http_client=client, review_status_floor=floor, limit=limit)

    return _run(handler, call)


def _span(
    handler: Callable[[httpx2.Request], httpx2.Response], *, gene: str = _GENE, limit: int = _POOL_LIMIT
) -> clinvar.ClinvarSpanRecords:
    """The span census over ``handler``, on one exon-sized interval."""

    async def call(client: httpx2.AsyncClient) -> clinvar.ClinvarSpanRecords:
        return await clinvar.fetch_span_records(gene, 31_232_881, 31_232_931, http_client=client, limit=limit)

    return _run(handler, call)


def _search_response(uids: list[str], total: int | None = None) -> httpx2.Response:
    return httpx2.Response(
        200, json={'esearchresult': {'idlist': uids, 'count': str(total if total is not None else len(uids))}}
    )


def test_fetch_variant_archive_reads_the_record_and_its_submissions() -> None:
    fetched = _archive('clinvar_vcv.xml')

    record = fetched.record
    assert record.clinvar_id == _VCV
    assert record.classification
    assert record.review_stars >= 1
    assert record.conditions
    # Star counts alone cannot support *_INF eligibility; each submission carries its own evidence.
    assert record.submissions
    assert all(s.scv.startswith('SCV') for s in record.submissions)
    assert all(s.submitter and s.review_status for s in record.submissions)
    assert any(s.assertion_method for s in record.submissions)
    assert any(s.comment for s in record.submissions)
    assert any(s.pubmed_ids for s in record.submissions)
    assert any(o.origin and o.collection_method for s in record.submissions for o in s.observations)


def test_the_archive_carries_the_two_facts_the_reading_cannot() -> None:
    """What the record is a record OF, which the aggregate classification does not say.

    A crosswalk entry can name a haplotype the queried allele is one part of, or a record ClinVar
    holds only because the allele arrived inside a larger submitted set.
    """
    fetched = _archive('clinvar_vcv.xml')

    assert fetched.variation_archive.accession == _VCV
    assert fetched.variation_archive.variation_type == 'single nucleotide variant'
    assert (
        fetched.variation_archive.record_type == clinvar_proto.clinvar_pb2.VariationArchiveType.RECORD_TYPE_CLASSIFIED
    )


def test_the_archive_is_fetched_by_accession_and_nothing_is_searched() -> None:
    """ClinVar indexes renderings, so the crosswalk's accession is the only identity claim there is."""
    fetched_ids: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if not request.url.path.endswith('/efetch.fcgi'):
            raise AssertionError('the archive lookup issues no search')
        fetched_ids.append(request.url.params['id'])
        return httpx2.Response(200, content=_VCV_XML)

    fetched = _run(handler, lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c))
    assert fetched_ids == [_VCV]
    assert _VCV in fetched.query


@pytest.mark.parametrize(
    'accession',
    [
        '1731988',  # the bare UID: efetch takes it with a 200 and an empty result set
        'VCV1731988',  # unpadded, so the same
        'VCV001731988.4',  # a record version, which this rpc does not answer about
        'NM_001042492.3:c.3496G>C',
        'CA398989536',
        '',
    ],
)
def test_an_accession_outside_the_padded_form_is_refused_before_the_fetch(accession: str) -> None:
    """Fetched, a bare UID answers with an empty set that reads as ClinVar holding no such record."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError('the accession should be refused before anything is fetched')

    with pytest.raises(errors.InvalidRequestError, match='zero-padded ClinVar variation accession'):
        _run(handler, lambda c: clinvar.fetch_variant_archive(accession, http_client=c))


@pytest.mark.parametrize(
    ('status', 'body', 'stated'),
    [
        (400, _NO_RECORD_ENVELOPE, 'ID list is empty'),
        (200, b'<ClinVarResult-Set><set/></ClinVarResult-Set>', 'empty result set'),
    ],
    ids=['a-refusal-naming-an-unresolved-id', 'an-empty-result-set'],
)
def test_the_two_ways_clinvar_says_it_holds_no_archive_are_one_disagreement(
    status: int, body: bytes, stated: str
) -> None:
    """One fact — ClinVar holds nothing under this accession — spelled two ways by one endpoint.

    Read apart, the refusal is a bad request and the empty set a fault, and the caller's next move
    differs between them for no reason it can see. Read as an absence, both become the novelty
    finding off an answer the crosswalk contradicts. So both are the two sources disagreeing, and
    the message carries the crosswalk's accession and ClinVar's own words for what it holds.
    """
    with pytest.raises(errors.InconsistentSourcesError, match=stated) as caught:
        _run(
            lambda _r: httpx2.Response(status, content=body),
            lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c),
        )

    said = str(caught.value)
    assert _VCV in said
    assert 'crosswalk' in said


def test_a_refusal_worded_otherwise_is_still_a_refusal() -> None:
    """Only ClinVar's own "no record" wording is an absence; another 400 is the call being refused.

    Read off the status alone, a fault in the request this adapter builds would come back as the
    queried accession not existing.
    """
    body = b'<eFetchResult><ERROR>Invalid db name specified: clnvar</ERROR></eFetchResult>'
    with pytest.raises(errors.InvalidRequestError, match='Invalid db name'):
        _run(
            lambda _r: httpx2.Response(400, content=body), lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c)
        )


@pytest.mark.parametrize(
    ('body', 'match'),
    [
        (b'<ClinVarResult-Set><VariationArchive/><VariationArchive/></ClinVarResult-Set>', 'returned 2 archives'),
        (b'<eFetchResult><ERROR>Empty id list</ERROR></eFetchResult>', 'not a'),
    ],
    ids=['two-archives', 'another-envelope'],
)
def test_a_200_that_is_not_one_archive_for_the_accession_fails_the_lookup(body: bytes, match: str) -> None:
    """An accession names one variation, and neither shape is ClinVar stating an absence.

    Read as one, either would become the novelty finding off an answer that never said so.
    """
    with pytest.raises(ValueError, match=match):
        _run(
            lambda _r: httpx2.Response(200, content=body), lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c)
        )


def test_the_gene_pool_reports_the_census_behind_it() -> None:
    """500 returned out of tens of thousands is indistinguishable from a complete pool without this."""
    retmax: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            retmax.append(request.url.params.get('retmax'))
        return _handler(request)

    pool = _pool(handler, limit=5)

    # Without retmax on the wire the pool silently caps at NCBI's default and the census lies.
    assert retmax == ['5']
    assert pool.total > pool.considered  # NF1's P/LP set runs to thousands; the page is 5
    assert pool.truncated
    assert len(pool.records) <= pool.considered <= pool.total


def test_the_aggregate_states_its_conditions_verbatim() -> None:
    """ClinVar's trait set carries "not provided" as one of its terms, and that is a fact about it.

    Normalised away, a record asserted against an unnamed condition reads as one asserted against
    the named conditions beside it, which is what the same-phenotype *_INF check compares on.
    """
    assert 'not provided' in _record('clinvar_vcv.xml').conditions


def test_a_submission_naming_its_condition_by_ontology_id_alone_carries_that_id() -> None:
    """A VCEP files an XRef and no name; read for names alone the submission asserts against nothing."""
    conditions = {s.scv: s.conditions for s in _record('clinvar_vcv.xml').submissions}

    assert conditions['SCV002617250'] == ['MedGen:CN230736', 'MedGen:C0027672']


def test_a_cohort_observed_at_several_zygosities_keeps_every_one() -> None:
    """One `ObservedIn` covers a cohort: HFE C282Y carries homozygotes and heterozygotes together."""
    mixed = [o for o in _observations(_record('clinvar_vcv_observations.xml')) if len(o.zygosities) > 1]

    assert mixed, 'the fixture no longer carries a mixed-zygosity cohort'
    for observation in mixed:
        assert len({z.zygosity for z in observation.zygosities}) == len(observation.zygosities)
        assert all(z.count is not None for z in observation.zygosities)


@pytest.mark.parametrize('fixture', ['clinvar_vcv_observations.xml', 'clinvar_vcv_expert_panel.xml'])
def test_a_homozygous_observation_is_carried_under_clinvars_own_token(fixture: str) -> None:
    """The homozygous observation the recessive rules most need is the one a spelling slip loses.

    ClinVar emits the `-zygote` noun; reading for the `-zygous` adjective matches nothing, and
    rewriting the token into one would state a count in a unit ClinVar's token did not name.
    """
    observed = {z.zygosity for o in _observations(_record(fixture)) for z in o.zygosities}

    assert 'Homozygote' in observed


@pytest.mark.parametrize(
    ('attributes', 'expected'),
    [
        ([('VariantAlleles', '124'), ('Homozygote', '5'), ('SingleHeterozygote', '119')], 124),
        ([('Homozygote', '5'), ('SingleHeterozygote', '119')], None),  # no VariantAlleles: unstated
        ([('SingleHeterozygote', '78')], None),
    ],
)
def test_variant_alleles_comes_only_from_the_attribute_of_that_name(
    attributes: list[tuple[str, str]], expected: int | None
) -> None:
    """A zygosity's count is individuals at that zygosity; borrowing it as the total invents a number."""
    observed_in = ET.Element('ObservedIn')
    for attribute_type, value in attributes:
        data = ET.SubElement(observed_in, 'ObservedData')
        ET.SubElement(data, 'Attribute', {'Type': attribute_type, 'integerValue': value})

    assert clinvar._observed_data(observed_in).variant_alleles == expected


def test_a_malformed_count_raises_rather_than_reading_as_unstated() -> None:
    observed_in = ET.Element('ObservedIn')
    data = ET.SubElement(observed_in, 'ObservedData')
    ET.SubElement(data, 'Attribute', {'Type': 'VariantAlleles', 'integerValue': 'many'})

    with pytest.raises(ValueError, match='non-integer integerValue'):
        clinvar._observed_data(observed_in)


def test_every_note_in_a_repeated_observation_block_is_kept() -> None:
    """`ObservedData` repeats: a literature submitter files one note per family it reports."""
    many = [o for o in _observations(_record('clinvar_vcv_observations.xml')) if len(o.descriptions) > 1]

    assert many, 'the fixture no longer carries a multi-note observation'
    assert all(text.strip() for o in many for text in o.descriptions)


def test_an_observation_carries_the_publication_it_came_from() -> None:
    """Which family an observation came from is what tells two submitters restating one paper apart."""
    cited = [o for o in _observations(_record('clinvar_vcv_observations.xml')) if o.pubmed_ids]

    assert cited
    assert all(pmid.isdigit() for o in cited for pmid in o.pubmed_ids)


def test_an_observation_carries_the_age_and_sex_a_submitter_stated() -> None:
    observations = _observations(_record('clinvar_vcv_observations.xml'))
    aged = [o for o in observations if o.age]

    assert aged
    # ClinVar states an age as a bounded range; a bare bound would read as a point value.
    assert all('minimum=' in o.age and 'maximum=' in o.age for o in aged)
    assert any(o.sex for o in observations)


def test_a_somatic_or_oncogenicity_assertion_is_not_germline_evidence() -> None:
    """Those SCVs answer a different question; their tumour observations are not germline evidence."""
    germline = ET.Element('ClinicalAssertion', {'ID': '1'})
    ET.SubElement(germline, 'ClinVarAccession', {'Accession': 'SCV1'})
    ET.SubElement(ET.SubElement(germline, 'Classification'), 'GermlineClassification').text = 'Pathogenic'
    somatic = ET.Element('ClinicalAssertion', {'ID': '2'})
    ET.SubElement(somatic, 'ClinVarAccession', {'Accession': 'SCV2'})
    ET.SubElement(ET.SubElement(somatic, 'Classification'), 'SomaticClinicalImpact').text = 'Tier I'

    assert clinvar._submission(germline) is not None
    assert clinvar._submission(somatic) is None


def test_a_registry_submission_without_a_classification_is_not_confused_with_one() -> None:
    """A germline element saying `not provided` is an answer; a missing one is a different question."""
    registry = ET.Element('ClinicalAssertion', {'ID': '3'})
    ET.SubElement(registry, 'ClinVarAccession', {'Accession': 'SCV3'})
    ET.SubElement(ET.SubElement(registry, 'Classification'), 'GermlineClassification').text = 'not provided'

    submission = clinvar._submission(registry)
    assert submission is not None
    assert submission.classification == ''


def test_an_assertion_without_an_accession_raises() -> None:
    assertion = ET.Element('ClinicalAssertion', {'ID': '4'})
    ET.SubElement(ET.SubElement(assertion, 'Classification'), 'GermlineClassification').text = 'Pathogenic'

    with pytest.raises(ValueError, match='no SCV accession'):
        clinvar._submission(assertion)


def test_an_expert_panel_curation_carries_its_evidence_repository_link() -> None:
    """A VCEP's erepo link is where its applied criteria are auditable, not just named."""
    record = _record('clinvar_vcv_expert_panel.xml')
    curated = [s for s in record.submissions if s.erepo_url]

    assert curated
    for submission in curated:
        assert submission.erepo_url.startswith('https://erepo.clinicalgenome.org/')
        assert submission.assertion_method
        assert submission.mode_of_inheritance


def test_a_submission_without_a_classification_keeps_its_observation() -> None:
    """A patient registry reports an observation under "no classification provided" — still evidence."""
    unclassified = [s for s in _record('clinvar_vcv_observations.xml').submissions if not s.classification]

    assert unclassified
    assert any(o.zygosities or o.age or o.origin for s in unclassified for o in s.observations)


def test_an_empty_gene_is_refused() -> None:
    """Without the gene clause the term returns ClinVar's whole P/LP set as if it were the gene's."""
    with pytest.raises(errors.InvalidRequestError, match='HGNC symbol'):
        _pool(_handler, gene='  ')


def test_a_complete_gene_pool_is_not_flagged_as_truncated() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(_FIXTURE['esearch_gene']['esearchresult']['idlist'])
        return httpx2.Response(200, json=_FIXTURE['esummary_gene'])

    pool = _pool(handler)
    assert not pool.truncated
    assert pool.considered == pool.total


def _pool_terms(floor: int) -> list[str]:
    """The esearch terms `fetch_gene_pool` issues at ``floor``."""
    terms: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            terms.append(request.url.params['term'])
        return _handler(request)

    _pool(handler, floor=floor)
    return terms


@pytest.mark.parametrize('floor', [1, 2, 3, 4])
def test_the_review_status_floor_scopes_the_search_itself(floor: int) -> None:
    """Applied after the bound it would only trim; in the term it is what reaches past the bound.

    A well-reviewed record ranked below `limit` is otherwise unreachable at any floor — on a
    well-studied gene, the record that settles the question.
    """
    admitted = {status for status, stars in clinvar._STAR_BY_REVIEW_STATUS.items() if status and stars >= floor}
    excluded = {status for status, stars in clinvar._STAR_BY_REVIEW_STATUS.items() if status and stars < floor}
    term = _pool_terms(floor)[0]
    assert all(f'"{status}"[Review status]' in term for status in admitted)
    assert not any(f'"{status}"[Review status]' in term for status in excluded)


def test_a_floor_of_zero_states_no_review_status_clause() -> None:
    """Every status qualifies, and one of them — a record with no germline call — has no phrase."""
    assert '[Review status]' not in _pool_terms(0)[0]


def test_the_pool_is_paged_rather_than_taken_as_one_page() -> None:
    """The bound is a record count, not a page size; NCBI caps a page below what a caller may ask."""
    pages: list[tuple[str, str]] = []
    uids = [str(i) for i in range(7)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            params = request.url.params
            start = int(params.get('retstart') or 0)
            retmax = int(params['retmax'])
            pages.append((params.get('retstart') or '0', params['retmax']))
            return _search_response(uids[start : start + min(retmax, 3)], total=len(uids))
        return httpx2.Response(200, json={'result': {'uids': []}})

    pool = _pool(handler, floor=0, limit=7)
    # Three pages of three, three and one: every matched record inside the bound is reached, and no
    # page boundary repeats or skips one.
    assert pool.considered == len(uids)
    assert not pool.truncated
    assert [start for start, _ in pages] == ['0', '3', '6']


def test_a_bound_below_the_match_count_reports_truncation() -> None:
    """The census is what tells a lower bound from a census; a bounded pool has to say so."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            retmax = int(request.url.params['retmax'])
            return _search_response([str(i) for i in range(retmax)], total=5000)
        return httpx2.Response(200, json={'result': {'uids': []}})

    pool = _pool(handler, limit=2)
    assert (pool.considered, pool.total, pool.truncated) == (2, 5000, True)


@pytest.mark.parametrize('limit', [0, -1])
def test_the_pool_refuses_a_bound_it_cannot_fetch_under(limit: int) -> None:
    with pytest.raises(ValueError, match='positive record bound'):
        _pool(_handler, limit=limit)


@pytest.mark.parametrize(
    ('review_status', 'stars'),
    [
        ('practice guideline', 4),
        ('reviewed by expert panel', 3),
        ('criteria provided, multiple submitters, no conflicts', 2),
        ('criteria provided, single submitter', 1),
        ('criteria provided, conflicting classifications', 1),
        ('no assertion criteria provided', 0),
        ('', 0),
    ],
)
def test_review_stars_mapping(review_status: str, stars: int) -> None:
    assert clinvar._review_stars(review_status) == stars


def test_an_unknown_review_status_raises() -> None:
    """A status read as 0 stars drops its record below every floor.

    Silently, and as if ClinVar had said the record was unreviewed. The floor is the caller's
    policy, so the star count it filters on has to be ClinVar's answer, not this map's fallback.
    """
    with pytest.raises(ValueError, match='unknown ClinVar review status'):
        clinvar._review_stars('reviewed by an expert panel')


def test_zero_star_records_are_excluded_from_the_gene_pool() -> None:
    summary = {
        'result': {
            'uids': ['1', '2'],
            '1': {
                'accession': 'VCV000000001',
                'title': 'kept',
                'germline_classification': {
                    'description': 'Pathogenic',
                    'review_status': 'criteria provided, single submitter',
                },
            },
            '2': {
                'accession': 'VCV000000002',
                'title': 'dropped',
                'germline_classification': {
                    'description': 'Pathogenic',
                    'review_status': 'no assertion criteria provided',
                },
            },
        }
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(['1', '2'])
        return httpx2.Response(200, json=summary)

    pool = _pool(handler)
    assert [r.clinvar_id for r in pool.records] == ['VCV000000001']


def test_a_pool_record_carries_the_coordinates_its_title_names() -> None:
    """Placing the pool against an exon table is the adapter's parse, not each caller's own regex.

    A gene pool holds copy-number records ClinVar titles cytogenetically; those name no c. span, and
    the record still comes back so the caller can count what it could not place.
    """
    titles = {'1': 'NM_007294.4(BRCA1):c.1521_1523delCTT', '2': 'GRCh38/hg38 17q21.31(chr17:43044295-43125364)x1'}
    summary = {
        'result': {
            'uids': list(titles),
            **{
                uid: {
                    'accession': f'VCV{uid}',
                    'title': title,
                    'germline_classification': {
                        'description': 'Pathogenic',
                        'review_status': 'criteria provided, single submitter',
                    },
                }
                for uid, title in titles.items()
            },
        }
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(list(titles))
        return httpx2.Response(200, json=summary)

    pool = _pool(handler)
    placed, unplaced = pool.records
    assert placed.coding_span is not None
    assert (placed.coding_span.transcript, placed.coding_span.start.position) == ('NM_007294.4', 1521)
    assert unplaced.coding_span is None


def _pool_over(
    descriptions: list[str],
    *,
    review_status: str = 'criteria provided, single submitter',
    floor: int = 1,
) -> clinvar.ClinvarGenePool:
    """The pool built from one record per supplied aggregate classification, at one review status."""
    uids = [str(i) for i, _ in enumerate(descriptions)]
    summary = {
        'result': {
            'uids': uids,
            **{
                uid: {
                    'accession': f'VCV{uid}',
                    'title': description,
                    'germline_classification': {'description': description, 'review_status': review_status},
                }
                for uid, description in zip(uids, descriptions, strict=True)
            },
        }
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(uids)
        return httpx2.Response(200, json=summary)

    return _pool(handler, floor=floor)


def test_the_caller_can_ask_for_a_pool_that_keeps_unreviewed_records() -> None:
    """A 0-star record can be a gene's whole frequency evidence (ACVRL1), so the floor is the caller's.

    A floor fixed in the adapter would make the library's required one a fiction: asking for 0 would
    still return a filtered pool, and no caller could tell.
    """
    pool = _pool_over(['Pathogenic'], review_status='no assertion criteria provided', floor=0)
    assert [r.review_stars for r in pool.records] == [0]


def test_only_pathogenic_classifications_enter_the_gene_pool() -> None:
    """A conflicting record is not a known P/LP variant, however the search term was indexed.

    The pool is what the DAFT and the informative-variant rules read as the gene's P/LP set, and
    "Conflicting classifications of pathogenicity" is what a substring test on "athogenic" lets in.
    """
    pool = _pool_over(
        [
            'Pathogenic',
            'Likely pathogenic',
            'Pathogenic/Likely pathogenic',
            'Conflicting classifications of pathogenicity',
            'Uncertain significance',
            'Benign',
        ]
    )
    assert [r.classification for r in pool.records] == [
        'Pathogenic',
        'Likely pathogenic',
        'Pathogenic/Likely pathogenic',
    ]


def test_a_qualified_or_tailed_record_enters_the_gene_pool() -> None:
    """The pool is the *_INF candidate set, whose per-variant eligibility SM19 assigns to the analyst.

    Excluding a reduced-penetrance or risk allele is SM3's frequency argument, applied over the pool
    by `frequency.known_pathogenic`; SM19 states no such condition, and a candidate dropped here is
    one the analyst never gets to judge.
    """
    admitted = [
        'Pathogenic, low penetrance',
        'Likely pathogenic, low penetrance',
        'Established risk allele',
        'Likely risk allele',
        'Pathogenic/Likely pathogenic; risk factor',
    ]
    pool = _pool_over([*admitted, 'Uncertain risk allele', 'Conflicting classifications of pathogenicity'])
    assert [r.classification for r in pool.records] == admitted


def test_the_entrez_property_spells_a_comma_bearing_term_without_its_comma() -> None:
    """The one upstream fact the derived search term rests on; a wrong spelling matches nothing.

    Entrez would not refuse the malformed clause either — it would return no record, reading back
    as ClinVar holding none of that classification for the gene.
    """
    assert clinvar._clinsig_property('pathogenic, low penetrance') == '"clinsig pathogenic low penetrance"[Properties]'


def test_an_unrecognised_classification_in_the_pool_is_a_fault() -> None:
    """Read as "not pathogenic", a renamed ClinVar term empties the pool with no signal that it did."""
    with pytest.raises(ValueError, match='unknown ClinVar germline classification term'):
        _pool_over(['Pathogenic', 'Probably pathogenic'])


def test_a_span_is_searched_by_coordinate_within_the_gene_and_filters_no_classification() -> None:
    """The *_INF rules score benign and VUS informative variants; a filtered span answers a different question."""
    # A B/LB record in the span is the defect's regression: `fetch_gene_pool` cannot hold one at any
    # floor, and the frameshift tree scores the first B at -2.0 and the first LB at -1.0.
    classifications = [
        'Benign',
        'Likely benign',
        'Uncertain significance',
        'Pathogenic',
        'Conflicting classifications of pathogenicity',
        '',  # a record with no germline classification at all: in the span, not an informative variant
    ]
    uids = [str(i) for i, _ in enumerate(classifications)]
    summary = {
        'result': {
            'uids': uids,
            **{
                uid: {
                    'accession': f'VCV{uid}',
                    'title': f'NM_001042492.3(NF1):c.{3496 + int(uid)}G>C',
                    'germline_classification': {
                        'description': description,
                        'review_status': 'criteria provided, single submitter' if description else '',
                    },
                }
                for uid, description in zip(uids, classifications, strict=True)
            },
        }
    }
    terms: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            terms.append(request.url.params['term'])
            return _search_response(uids)
        return httpx2.Response(200, json=summary)

    found = _span(handler)
    # Two clauses: the endpoint index, plus the long records near the span that could contain it.
    assert len(terms) == 1
    assert '31232881:31232931[Base Position]' in terms[0]
    assert '[Length of the variant]' in terms[0]
    assert terms[0].startswith(f'{_GENE}[gene] AND ')
    assert [r.classification for r in found.records] == classifications
    assert found.considered == found.total == len(classifications)
    assert not found.truncated
    assert found.query == terms[0]


def test_a_span_without_a_gene_is_refused() -> None:
    """Unscoped, `[Base Position]` matches the same coordinates on every chromosome."""
    with pytest.raises(errors.InvalidRequestError, match='empty gene'):
        _span(_handler, gene='  ')


def _span_length_bounds(start: int, end: int) -> tuple[int, int]:
    """The `[Length of the variant]` range the span search over `[start, end]` states, as (low, high)."""
    terms: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith('/esearch.fcgi')
        term = request.url.params['term']
        terms.append(term)
        return _search_response([], total=0 if '[Base Position]' in term else 5866)

    async def call(client: httpx2.AsyncClient) -> clinvar.ClinvarSpanRecords:
        return await clinvar.fetch_span_records(_GENE, start, end, http_client=client, limit=_POOL_LIMIT)

    _run(handler, call)
    stated = re.search(r'(\d+):(\d+)\[Length of the variant\]', terms[0])
    assert stated, f'the span term states no length clause: {terms[0]!r}'
    return int(stated[1]), int(stated[2])


def test_a_span_wider_than_the_pad_states_a_length_range_that_holds_it() -> None:
    """A CDS range crossing an intron is kilobases wide, and a record containing it is wider still.

    Both ends of the range are measured from the span, so it stays ascending however wide the span
    is and always admits a record of exactly the span's length. A range the span's own length falls
    outside reaches no containing record at all, and that census reads as "no informative variant".
    """
    start, end = 31_232_881, 31_236_880  # 4000 bases: four times `_SPAN_PAD`
    low, high = _span_length_bounds(start, end)

    assert low <= end - start + 1 <= high


def _empty_span(matches_the_gene: int) -> Callable[[httpx2.Request], httpx2.Response]:
    """A transport whose span search matches nothing and whose gene-only probe matches `n`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith('/esearch.fcgi')
        matched = matches_the_gene if '[Base Position]' not in request.url.params['term'] else 0
        return _search_response([], total=matched)

    return handler


def _span_over(records: list[tuple[str, int, int]], *, start: int, end: int) -> clinvar.ClinvarSpanRecords:
    """The span census over records the summary places at `(start, stop)` on GRCh38."""
    uids = [str(i) for i, _ in enumerate(records)]
    summary = {
        'result': {
            'uids': uids,
            **{
                uid: {
                    'accession': f'VCV{uid}',
                    'title': title,
                    'germline_classification': {
                        'description': 'Likely benign',
                        'review_status': 'criteria provided, single submitter',
                    },
                    'variation_set': [
                        {
                            'variation_loc': [
                                {'assembly_name': 'GRCh38', 'start': str(lo), 'stop': str(hi)},
                                {'assembly_name': 'GRCh37', 'start': '1', 'stop': '1'},
                            ]
                        }
                    ],
                }
                for uid, (title, lo, hi) in zip(uids, records, strict=True)
            },
        }
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(uids)
        return httpx2.Response(200, json=summary)

    async def call(client: httpx2.AsyncClient) -> clinvar.ClinvarSpanRecords:
        return await clinvar.fetch_span_records(_GENE, start, end, http_client=client, limit=_POOL_LIMIT)

    return _run(handler, call)


def test_a_record_spanning_the_range_without_an_endpoint_in_it_is_kept() -> None:
    """The record the endpoint index cannot reach, and the one that most matters.

    ClinVar files a record at its two endpoints, so a deletion covering a codon is found only by the
    second clause — and it is an informative variant at that codon.
    """
    found = _span_over([('NM_1.1:c.1105_1115del', 1000, 1010), ('NM_1.1:c.1108T>G', 1004, 1004)], start=1004, end=1006)
    assert [r.clinvar_id for r in found.records] == ['VCV0', 'VCV1']


def test_a_long_record_near_the_range_but_not_meeting_it_is_dropped() -> None:
    """The second clause reaches a wider window, so a hit has to be measured rather than assumed."""
    found = _span_over([('NM_1.1:c.900_950del', 800, 850)], start=1004, end=1006)
    assert found.records == []
    # The census still reports what the term matched, so the filtering is visible rather than silent.
    assert found.total == found.considered == 1


def test_a_record_stating_no_span_on_the_searched_assembly_is_kept() -> None:
    """It was matched at these coordinates; dropping it would cut the census on an unfilled field."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(['0'])
        return httpx2.Response(
            200,
            json={
                'result': {
                    'uids': ['0'],
                    '0': {
                        'accession': 'VCV0',
                        'title': 'NM_1.1:c.1108T>G',
                        'germline_classification': {
                            'description': 'Benign',
                            'review_status': 'no assertion criteria provided',
                        },
                        'variation_set': [{'variation_loc': [{'assembly_name': 'GRCh37', 'start': '1', 'stop': '1'}]}],
                    },
                }
            },
        )

    async def call(client: httpx2.AsyncClient) -> clinvar.ClinvarSpanRecords:
        return await clinvar.fetch_span_records(_GENE, 1004, 1006, http_client=client, limit=_POOL_LIMIT)

    assert [r.clinvar_id for r in _run(handler, call).records] == ['VCV0']


def test_an_empty_span_of_a_gene_clinvar_indexes_is_the_finding() -> None:
    """Most codons carry no ClinVar record; that is the answer the *_INF rules are asked for."""
    found = _span(_empty_span(matches_the_gene=5866))
    assert found.records == []
    assert (found.total, found.considered, found.truncated) == (0, 0, False)


def test_an_empty_span_of_a_symbol_clinvar_does_not_index_is_not_the_finding() -> None:
    """The two are the same empty answer, and one of them is "no informative variant at this codon".

    The symbol comes from the transcript's exon table rather than the caller, so it goes wrong the
    way two annotation sources disagree about a gene's name — silently, and only on this path.
    """
    with pytest.raises(errors.InconsistentSourcesError, match='indexes no record under gene'):
        _span(_empty_span(matches_the_gene=0))


def test_a_descending_span_is_refused() -> None:
    async def call(client: httpx2.AsyncClient) -> clinvar.ClinvarSpanRecords:
        return await clinvar.fetch_span_records(_GENE, 900, 800, http_client=client, limit=10)

    with pytest.raises(ValueError, match='ascending'):
        _run(_handler, call)


def test_malformed_esummary_raises() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/esearch.fcgi'):
            return _search_response(['1'])
        return httpx2.Response(200, json={'unexpected': 'shape'})  # no 'result'

    with pytest.raises(ValueError, match='no result object'):
        _pool(handler)


def test_an_esearch_without_a_count_raises() -> None:
    """The count is the truncation signal; a response missing it cannot be silently read as complete."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={'esearchresult': {'idlist': ['1']}})

    with pytest.raises(ValueError, match='no count'):
        _pool(handler)


@pytest.mark.parametrize(
    'body',
    [
        b'<!DOCTYPE r [<!ENTITY a "expanded">]><ClinVarResult-Set><VariationArchive>&a;</VariationArchive>',
        b'<ClinVarResult-Set><VariationArchive',
    ],
    ids=['an-entity-expansion', 'truncated-xml'],
)
def test_efetch_xml_the_parser_refuses_surfaces_as_a_parse_failure(body: bytes) -> None:
    """Entity expansion is the attack the parser is hardened against; its refusal must not read as an absence."""
    with pytest.raises(ValueError, match='unparsable XML'):
        _run(
            lambda _r: httpx2.Response(200, content=body), lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c)
        )


def test_non_2xx_raises() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _pool(lambda _r: httpx2.Response(429, json={}))


def test_placeholder_submitter_fields_are_not_carried_as_values() -> None:
    """ClinVar renders "not provided" as if it were an answer; treating it as one is a wrong fact."""
    blanked = _VCV_XML.replace(b'<AffectedStatus>unknown', b'<AffectedStatus>not provided')

    fetched = _run(
        lambda _r: httpx2.Response(200, content=blanked), lambda c: clinvar.fetch_variant_archive(_VCV, http_client=c)
    )
    observations = _observations(fetched.record)
    assert observations
    placeholders = {'not provided', 'none provided'}
    assert all(o.affected_status.lower() not in placeholders for o in observations)
    assert all(text.lower() not in placeholders for o in observations for text in o.descriptions)
