"""CSpec adapter: the registry traversal, the lifecycle derivation, and what it refuses to guess.

The happy path runs over a committed registry payload — ACTC1's gene record, the Cardiomyopathy
panel's GN101 document trimmed to three criteria, and the rule set behind it — so the traversal and
the criterion parsing are exercised against the shape the service really returns. Everything else is
built from small payloads, because the cases worth pinning are shapes the registry produces rarely
(a field stated as a string on one record and a list on another) or lifecycle states one gene cannot
show at once. No test hits the network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
from collections.abc import Awaitable, Callable, Mapping

import httpx
import pytest

from themis.rpc import cspec_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import cspec

_FIXTURE = json.loads((pathlib.Path(__file__).resolve().parent / 'fixtures' / 'cspec.json').read_bytes())
_GENE = 'ACTC1'
_SPECIFICATION = 'GN101'
_MISSING = {'status': {'code': 404, 'name': 'Not Found', 'msg': "Bad Entity - No 'Gene' entity found"}}

# One JSON object of the committed payload. `object` values, narrowed at each descent by the helpers
# below, so a fixture reshaped under a test fails there rather than silently indexing something else.
type Record = dict[str, object]


def _entity_key(request: httpx.Request) -> tuple[str, str]:
    """The (entity type, identifier) a registry URL names."""
    entity_type, _, identifier = request.url.path.removeprefix('/cspec/').partition('/id/')
    return entity_type, identifier


def _handler(
    records: Mapping[tuple[str, str], Record], seen: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each entity lookup from `records`, 404-ing anything the map does not hold."""

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        record = records.get(_entity_key(request))
        if record is None:
            return httpx.Response(404, json=_MISSING)
        return httpx.Response(200, json={'data': record, 'status': {'code': 200, 'name': 'OK'}})

    return handle


def _run[T](handler: Callable[[httpx.Request], httpx.Response], call: Callable[[httpx.AsyncClient], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _record(value: object) -> Record:
    """A JSON object read back out of the committed payload, narrowed for indexing."""
    assert isinstance(value, dict)
    return value


def _records(value: object) -> list[Record]:
    assert isinstance(value, list)
    return [_record(entry) for entry in value]


def _linked(document: Record, entity_type: str) -> list[Record]:
    """One kind of linked entity a document carries."""
    return _records(_record(document['ld'])[entity_type])


def _content(record: Record) -> Record:
    return _record(record['entContent'])


def _copy(record: Record) -> Record:
    """A deep copy of a committed record, for a test that mutates one field of it."""
    return _record(copy.deepcopy(record))


def _specification_record() -> Record:
    return _copy(_record(_FIXTURE['specification']))


def _registry(
    *, gene: Record | None = None, specification: Record | None = None, rule_set: Record | None = None
) -> dict[tuple[str, str], Record]:
    """The three committed records, with any of them replaced."""
    document = specification if specification is not None else _record(_FIXTURE['specification'])
    held: dict[tuple[str, str], Record] = {
        ('Gene', _GENE): gene if gene is not None else _record(_FIXTURE['gene']),
        ('SequenceVariantInterpretation', _SPECIFICATION): document,
    }
    for embedded in _linked(document, 'RuleSet'):
        identifier = embedded['ldhId']
        assert isinstance(identifier, str)
        held[('RuleSet', identifier)] = rule_set if rule_set is not None else _record(_FIXTURE['rule_set'])
    return held


def _fetch(
    records: Mapping[tuple[str, str], Record], gene: str = _GENE, seen: list[httpx.Request] | None = None
) -> cspec.CspecResult:
    return _run(_handler(records, seen), lambda c: cspec.fetch_criteria_specifications(gene, http_client=c))


def _criterion(result: cspec.CspecResult, code: str) -> cspec_pb2.CriterionSpecification:
    return next(criterion for criterion in result.specifications[0].criteria if criterion.code == code)


def _criteria_records(document: Record) -> list[Record]:
    return _linked(document, 'CriteriaCode')


def _strength_records(criterion: Record) -> list[Record]:
    return _records(_content(criterion)['strengthDescriptor'])


def test_a_gene_reaches_the_specification_its_rule_set_names() -> None:
    result = _fetch(_registry())
    assert result.coverage == cspec_pb2.SPECIFICATION_COVERAGE_SPECIFIED
    (specification,) = result.specifications
    assert specification.id == _SPECIFICATION
    assert specification.criteria, 'a specification with no criterion carries nothing to read'
    assert {entity.gene for entity in specification.entities} == {_GENE}


def test_every_request_asks_for_the_content_level_that_carries_the_criteria() -> None:
    # Below `high` the registry omits `entContent` from every linked entity and still answers 200,
    # so a request that did not ask would return a document with no criterion content at all.
    seen: list[httpx.Request] = []
    _fetch(_registry(), seen=seen)
    assert seen, 'the traversal issued no request'
    assert all(request.url.params.get('detail') == 'high' for request in seen)


def test_one_provenance_query_per_request_issued() -> None:
    seen: list[httpx.Request] = []
    result = _fetch(_registry(), seen=seen)
    assert [query.query for query in result.queries] == [str(request.url.copy_with(params=None)) for request in seen]
    # The document's own version is what a fact read off it rests on; the gene lookup is about no
    # document and states none.
    versions = {query.dataset_versions for query in result.queries}
    assert versions == {(), (f'{_SPECIFICATION} {result.specifications[0].citation.version}',)}


def test_a_criterion_the_panel_excludes_is_returned_saying_so() -> None:
    """The ACTC1 determination: reading it is what separates a scored null variant from an unscored one."""
    pvs1 = _criterion(_fetch(_registry()), 'PVS1')
    assert pvs1.applicability == cspec_pb2.APPLICABILITY_NOT_APPLICABLE
    assert pvs1.additional_comments, 'the panel states why it excludes the criterion; an empty note loses that'
    assert {strength.applicability for strength in pvs1.strengths} == {cspec_pb2.APPLICABILITY_NOT_APPLICABLE}


_LADDER = ['Stand Alone', 'Very Strong', 'Strong', 'Moderate', 'Supporting']


def test_every_criterion_carries_the_whole_strength_ladder() -> None:
    """An absent entry would read as a strength the specification says nothing about."""
    for criterion in _fetch(_registry()).specifications[0].criteria:
        assert [strength.strength for strength in criterion.strengths] == _LADDER
        assert all(strength.applicability != cspec_pb2.APPLICABILITY_UNSPECIFIED for strength in criterion.strengths)


def test_the_strength_ladder_is_ordered_by_the_service_not_by_the_registrys_array() -> None:
    # The registry lists Supporting ahead of Moderate on some criteria, so a caller reading a rung by
    # position would read the wrong one on those.
    document = _specification_record()
    for record in _criteria_records(document):
        content = _content(record)
        content['strengthDescriptor'] = list(reversed(_records(content['strengthDescriptor'])))
    for criterion in _fetch(_registry(specification=document)).specifications[0].criteria:
        assert [strength.strength for strength in criterion.strengths] == _LADDER


def test_a_strength_outside_the_ladder_fails_rather_than_sorting_to_one_end() -> None:
    document = _specification_record()
    _strength_records(_criteria_records(document)[0])[0]['strength'] = 'Extremely Strong'
    with pytest.raises(ValueError, match='Extremely Strong'):
        _fetch(_registry(specification=document))


def test_the_citation_carries_a_version_specific_doi_and_the_approval_date() -> None:
    citation = _fetch(_registry()).specifications[0].citation
    assert citation.document_doi
    # The concept DOI resolves to whatever the newest version is, so a citation resting on it names a
    # document that changes under the quote.
    assert citation.document_doi != citation.concept_doi
    assert citation.version
    assert citation.HasField('approved_on')
    assert citation.registry_url.endswith(_SPECIFICATION)


def test_the_panel_its_citations_and_its_attachments_are_named() -> None:
    specification = _fetch(_registry()).specifications[0]
    assert specification.expert_panel
    # The route to a panel's stated derivation: a threshold's warrant is usually a cited paper, and
    # its PMID goes straight to the literature service.
    assert [reference.id for reference in specification.references if reference.namespace == 'pmid']
    # A criterion pointing at an attached decision tree needs the attachment named, since the
    # registry serves no bytes for it.
    assert [attachment.file_name for attachment in specification.attachments]


def test_a_document_two_panels_publish_is_refused_rather_than_attributed_to_one() -> None:
    # The panel is the attribution a quotation carries, and picking one of two would put a
    # misattribution into a citation with nothing marking it as a choice.
    document = _specification_record()
    organizations = _record(document['ldFor'])['Organization']
    assert isinstance(organizations, list)
    _record(document['ldFor'])['Organization'] = [*organizations, *organizations]
    with pytest.raises(ValueError, match='2 organizations'):
        _fetch(_registry(specification=document))


def test_the_combining_rules_stay_untyped_in_raw() -> None:
    # Typing an ACMG/AMP-2015 combining algorithm would invite running it against an SVCv4 tally.
    rule_sets = _fetch(_registry()).raw['rule_sets']
    assert isinstance(rule_sets, dict)
    assert 'rules' in rule_sets[_SPECIFICATION][0]['entContent']


def test_a_symbol_the_registry_holds_no_entry_for_is_its_own_answer() -> None:
    # Distinct from "no panel has specified this gene": one is about the caller's symbol, the other
    # is the finding that SM3's first DAFT method is unavailable.
    result = _fetch({})
    assert result.coverage == cspec_pb2.SPECIFICATION_COVERAGE_GENE_ABSENT
    assert not result.specifications
    assert len(result.queries) == 1, 'a symbol with no entry has nothing to traverse'


def test_a_gene_no_specification_names_is_a_fact_not_an_error() -> None:
    gene = _copy(_record(_FIXTURE['gene']))
    gene['ldFor'] = {}
    result = _fetch(_registry(gene=gene))
    assert result.coverage == cspec_pb2.SPECIFICATION_COVERAGE_NO_SPECIFICATION
    assert not result.specifications


def test_a_candidate_whose_rule_set_does_not_name_the_gene_is_dropped_and_named() -> None:
    """The registry cross-links a gene to documents that do not specify it — GN014 reaches MECP2."""
    document = _specification_record()
    _content(_linked(document, 'RuleSet')[0])['genes'] = [{'gene': 'OTHER'}]
    result = _fetch(_registry(specification=document))
    assert result.coverage == cspec_pb2.SPECIFICATION_COVERAGE_NO_SPECIFICATION
    assert result.raw['candidate_specifications'] == [{'id': _SPECIFICATION, 'specifies_gene': False}]


def test_a_multi_gene_document_returns_only_the_requested_gene_s_criteria() -> None:
    document = _specification_record()
    excluded, included = (_content(record) for record in _criteria_records(document)[:2])
    excluded['gene'] = ['OTHER']
    included['gene'] = [_GENE, 'OTHER']
    kept = {criterion.code for criterion in _fetch(_registry(specification=document)).specifications[0].criteria}
    assert excluded['label'] not in kept
    assert included['label'] in kept


def test_a_criterion_scoped_to_no_gene_is_kept() -> None:
    # How a single-gene document states every one of its criteria; filtering them out would answer
    # that the panel specified nothing.
    document = _specification_record()
    for record in _criteria_records(document):
        _content(record).pop('gene', None)
    assert _fetch(_registry(specification=document)).specifications[0].criteria


@pytest.mark.parametrize(
    ('states', 'legacy_replaced', 'fully_superseded', 'expected'),
    [
        ([{'name': 'Released', 'current': True}], None, None, cspec_pb2.SPECIFICATION_STATUS_IN_FORCE),
        ([{'name': 'Released', 'current': True}], True, None, cspec_pb2.SPECIFICATION_STATUS_REPLACED),
        ([{'name': 'Released', 'current': True}], None, False, cspec_pb2.SPECIFICATION_STATUS_NOT_YET_EFFECTIVE),
        ([{'name': 'Released', 'current': True}], None, True, cspec_pb2.SPECIFICATION_STATUS_IN_FORCE),
        ([{'name': 'CSpec Deleted', 'current': True}], None, None, cspec_pb2.SPECIFICATION_STATUS_UNRELEASED),
        ([{'name': 'Pilot Rules In Prep', 'current': True}], None, False, cspec_pb2.SPECIFICATION_STATUS_UNRELEASED),
    ],
)
def test_the_lifecycle_says_whether_a_document_is_in_force(
    states: list[Record], legacy_replaced: bool | None, fully_superseded: bool | None, expected: int
) -> None:
    """A draft and a superseded document read exactly like an approved one without this."""
    document = _specification_record()
    content = _content(document)
    content['states'] = states
    if legacy_replaced is not None:
        content['legacyReplaced'] = legacy_replaced
    if fully_superseded is not None:
        content['legacyFullySuperseded'] = fully_superseded
    specification = _fetch(_registry(specification=document)).specifications[0]
    assert specification.status == expected
    assert specification.state == states[0]['name']


@pytest.mark.parametrize('term', ['Not applicable', 'Not Applicable', 'Not Applicable for this VCEP'])
def test_one_determination_spelled_several_ways_lands_on_one_value(term: str) -> None:
    # The registry spells this in two letter cases, so a caller comparing the term as a string would
    # read a criterion the panel excluded as one it uses.
    document = _specification_record()
    _content(_criteria_records(document)[0])['applicability'] = term
    criterion = _fetch(_registry(specification=document)).specifications[0].criteria[0]
    assert criterion.applicability == cspec_pb2.APPLICABILITY_NOT_APPLICABLE
    assert criterion.applicability_term == term


def test_an_applicability_term_the_service_does_not_encode_fails_the_fetch() -> None:
    document = _specification_record()
    _content(_criteria_records(document)[0])['applicability'] = 'Applicable on alternate Tuesdays'
    with pytest.raises(ValueError, match='applicability'):
        _fetch(_registry(specification=document))


@pytest.mark.parametrize('stated', ['one instruction', ['one instruction']])
def test_a_field_the_registry_states_two_ways_reads_one_way(stated: str | list[str]) -> None:
    document = _specification_record()
    _content(_criteria_records(document)[0])['instructionsToUse'] = stated
    assert list(_fetch(_registry(specification=document)).specifications[0].criteria[0].instructions) == [
        'one instruction'
    ]


@pytest.mark.parametrize(
    ('stated', 'expected'), [(4, '4'), (-1, '-1'), ('Not Applicable', 'Not Applicable'), (None, '')]
)
def test_the_panel_s_own_points_stay_verbatim_strings(stated: int | str | None, expected: str) -> None:
    # The registry states this as an integer on most records and as a phrase on others, and it is
    # another framework's point value either way.
    document = _specification_record()
    descriptor = _strength_records(_criteria_records(document)[0])[0]
    descriptor.pop('defaultPoint', None)
    if stated is not None:
        descriptor['defaultPoint'] = stated
    strengths = _fetch(_registry(specification=document)).specifications[0].criteria[0].strengths
    assert strengths[0].default_points == expected


def test_points_stated_as_a_shape_the_service_cannot_read_fail_the_fetch() -> None:
    document = _specification_record()
    _strength_records(_criteria_records(document)[0])[0]['defaultPoint'] = {'value': 4}
    with pytest.raises(ValueError, match='neither an integer nor a string'):
        _fetch(_registry(specification=document))


def test_a_note_the_registry_misspells_the_key_of_is_still_read() -> None:
    # Three spellings of one field are live in the registry, and the criteria carry no raw record of
    # their own — so reading only the correct one drops a panel's note with no absence visible.
    document = _specification_record()
    content = _content(_criteria_records(document)[0])
    content.pop('additionalComments', None)
    content['additonalComments'] = 'the panel said this'
    assert _fetch(_registry(specification=document)).specifications[0].criteria[0].additional_comments == (
        'the panel said this'
    )


def test_a_strength_note_the_registry_misspells_the_key_of_is_still_read() -> None:
    # Same rationale as the criterion-level case: the strengths are fully typed and carry no raw
    # record, so reading only the correct spelling drops a panel's caveats invisibly.
    document = _specification_record()
    descriptor = _strength_records(_criteria_records(document)[0])[0]
    descriptor.pop('additionalComments', None)
    descriptor['additonalComments'] = {'Caveats': ['unreachable under the correct spelling']}
    notes = _fetch(_registry(specification=document)).specifications[0].criteria[0].strengths[0].notes
    assert [note.heading for note in notes] == ['Caveats']


def test_a_headed_note_block_keeps_its_heading() -> None:
    document = _specification_record()
    _strength_records(_criteria_records(document)[0])[0]['additionalComments'] = {'Caveats': ['first', 'second']}
    notes = _fetch(_registry(specification=document)).specifications[0].criteria[0].strengths[0].notes
    assert [(note.heading, list(note.lines)) for note in notes] == [('Caveats', ['first', 'second'])]


def test_a_document_the_gene_links_to_and_the_registry_does_not_hold_is_a_fault() -> None:
    # The gene's own record named it, so a miss here is the registry contradicting itself. It must
    # NOT reach the caller as INVALID_ARGUMENT: that says the gene symbol was wrong, and the guest's
    # retry helper treats it as settled — burying a registry inconsistency a retry might clear.
    held = _registry()
    del held[('SequenceVariantInterpretation', _SPECIFICATION)]
    with pytest.raises(ValueError, match=_SPECIFICATION) as caught:
        _fetch(held)
    assert not isinstance(caught.value, errors.InvalidRequestError)


def test_a_symbol_carrying_url_syntax_cannot_reach_the_registry_as_another_gene() -> None:
    # Unencoded, a `#` truncates the path client-side: the registry answers about the prefix, the
    # rule-set filter misses, and a mistyped symbol comes back as "no panel has specified this gene".
    seen: list[httpx.Request] = []
    result = _fetch(_registry(), gene=f'{_GENE}#x', seen=seen)
    # `raw_path`, not `path`: httpx decodes the latter, so only the wire form shows the encoding.
    assert [request.url.raw_path for request in seen] == [f'/cspec/Gene/id/{_GENE}%23x?detail=high'.encode()]
    assert result.coverage == cspec_pb2.SPECIFICATION_COVERAGE_GENE_ABSENT


def test_a_released_document_the_panel_has_reopened_is_not_in_force() -> None:
    """Live on ATM, PALB2 and SERPINC1: released, dated, DOI'd, and back in review.

    The registry's own published recipe tests the state history alone and calls these current
    releases, which is the misattribution the status enum exists to prevent.
    """
    document = _specification_record()
    _content(document)['states'] = [
        {'name': 'Released', 'current': False},
        {'name': 'Pilot Rules Submitted', 'current': True},
    ]
    specification = _fetch(_registry(specification=document)).specifications[0]
    assert specification.status == cspec_pb2.SPECIFICATION_STATUS_RELEASED_UNDER_REVISION
    assert specification.state == 'Pilot Rules Submitted'


@pytest.mark.parametrize('flag', ['legacyReplaced', 'legacyFullySuperseded'])
def test_a_legacy_flag_stated_as_a_string_fails_rather_than_reading_as_in_force(flag: str) -> None:
    # The two flags decide whether a document's text still governs; read untyped, "true" is not True
    # and a superseded document quietly returns to IN_FORCE.
    document = _specification_record()
    _content(document)[flag] = 'true'
    with pytest.raises(ValueError, match=flag):
        _fetch(_registry(specification=document))


def test_a_released_document_stating_no_criterion_fails_rather_than_answering() -> None:
    # The shape an under-detailed read of the registry produces behind a 200; answering it would
    # report a panel that published criteria as having published none.
    document = _specification_record()
    _record(document['ld'])['CriteriaCode'] = []
    with pytest.raises(ValueError, match='states no criterion'):
        _fetch(_registry(specification=document))


def test_a_draft_stating_no_criterion_is_returned_as_the_draft_it_is() -> None:
    document = _specification_record()
    _content(document)['states'] = [{'name': 'Pilot Rules In Prep', 'current': True}]
    _record(document['ld'])['CriteriaCode'] = []
    specification = _fetch(_registry(specification=document)).specifications[0]
    assert specification.status == cspec_pb2.SPECIFICATION_STATUS_UNRELEASED
    assert not specification.criteria


def test_an_incomplete_strength_ladder_fails_rather_than_returning_the_rungs_it_has() -> None:
    # A missing rung reads as a strength the specification is silent about, not one it excludes.
    document = _specification_record()
    content = _content(_criteria_records(document)[0])
    content['strengthDescriptor'] = _records(content['strengthDescriptor'])[:3]
    with pytest.raises(ValueError, match='not the ladder'):
        _fetch(_registry(specification=document))


def test_a_strength_stating_no_applicability_fails() -> None:
    document = _specification_record()
    _strength_records(_criteria_records(document)[0])[0]['applicability'] = ''
    with pytest.raises(ValueError, match='states no applicability'):
        _fetch(_registry(specification=document))


def test_a_document_no_panel_publishes_is_refused_rather_than_left_unattributed() -> None:
    document = _specification_record()
    _record(document['ldFor'])['Organization'] = []
    with pytest.raises(ValueError, match='no publishing organization'):
        _fetch(_registry(specification=document))


def test_the_record_each_status_was_derived_from_reaches_raw() -> None:
    # `status` is the only derived leaf a caller cannot check against a returned field otherwise.
    specifications = _fetch(_registry()).raw['specifications']
    assert isinstance(specifications, dict)
    assert 'states' in _record(specifications[_SPECIFICATION])


def test_a_record_whose_shape_is_not_one_the_adapter_reads_fails_rather_than_returning_less() -> None:
    document = _specification_record()
    _record(document['ld'])['CriteriaCode'] = {'not': 'a list'}
    with pytest.raises(ValueError, match='not a list'):
        _fetch(_registry(specification=document))


def test_a_success_carrying_no_data_object_is_a_shape_change() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'status': {'code': 200, 'name': 'OK'}})

    with pytest.raises(ValueError, match='no data object'):
        _run(handle, lambda c: cspec.fetch_criteria_specifications(_GENE, http_client=c))


def test_the_routers_own_error_envelope_reaches_the_caller_as_its_message() -> None:
    # The registry answers a missing entity under `status.msg` and its router answers an unparseable
    # path under `errMsg`, in a body carrying no `status` at all.
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={'errCode': 400, 'errMsg': 'INVALID URL', 'errName': 'Bad Request'})

    with pytest.raises(errors.InvalidRequestError, match='INVALID URL'):
        _run(handle, lambda c: cspec.fetch_criteria_specifications(_GENE, http_client=c))
