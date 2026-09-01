"""ClinGen CSpec Registry adapter: a gene's VCEP criteria specifications, criterion by criterion.

The registry is a linked-data store, not a gene-keyed index, so reaching a gene's specifications is a
traversal and each hop has a trap in it:

1. ``/cspec/Gene/id/{symbol}`` — keyed on HGNC's approved symbol and CASE-SENSITIVE; the table holds
   every HGNC gene, so a 404 is a symbol the registry has no entry for rather than a gene no panel
   specified. The two are different answers and this adapter keeps them apart.
2. That record's ``ldFor.SequenceVariantInterpretation`` — candidates, not answers. The registry
   cross-links a gene to documents that do not specify it: the two legacy documents GN014 and GN016
   each link all ten genes of both panels, so MECP2 reaches a mitochondrial specification.
3. Each candidate document, whose ``ld.RuleSet[].entContent.genes[].gene`` is what actually says
   which genes it specifies. That is the filter, and what it drops is reported rather than dropped
   quietly.
4. The document's ``ld.CriteriaCode`` — one record per (criterion, gene), scoped by its own ``gene``
   list on a multi-gene document.
5. The rule set's own record, for the files a panel attaches (a PVS1 decision tree, an appendix).
   They hang off the rule set, and a document's embedded copy of it does not carry them.

``detail=high`` is passed on every request. The service's documented default is ``med``, which omits
``entContent`` from every linked entity — that is every criterion's content — while still answering
200, so relying on the default the endpoint happens to have would degrade silently.

Two error envelopes: the registry answers a missing entity with ``{"status": {"code": 404, …}}`` and
its router answers a path it cannot parse with ``{"errCode": 400, …}``, which carries no ``status``
at all. Neither is a 200 hiding a verdict, so the shared ``errors.raise_for_status`` rule applies —
except on the gene lookup's 404, which is an answer (see step 1).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence

import httpx2
from google.protobuf import timestamp_pb2

from themis.rpc import cspec_pb2
from themis.services.evidence import errors

_BASE_URL = 'https://cspec.genome.network/cspec'
_SOURCE = 'ClinGen CSpec Registry'
# The registry's rendered document page. Its own disclaimer calls the service's IRIs non-permalinks,
# so this is a convenience link and the Zenodo DOI is what a citation rests on.
_DOCUMENT_URL = 'https://cspec.genome.network/cspec/ui/svi/doc/{specification}'
_ENTITY_URL = f'{_BASE_URL}/{{entity_type}}/id/{{identifier}}'
# Every linked entity's `entContent` is omitted below this level, criteria content included.
_DETAIL = {'detail': 'high'}

_SPECIFICATION_TYPE = 'SequenceVariantInterpretation'
_RELEASED_STATE = 'Released'

# The ACMG/AMP-2015 strength ladder, strongest first. The registry's own array order is not it (see
# `_strengths`), so this is what the returned order is imposed from.
_STRENGTH_LADDER = ('Stand Alone', 'Very Strong', 'Strong', 'Moderate', 'Supporting')

# The registry writes one determination in several spellings, two of them differing only in letter
# case ("Not applicable" / "Not Applicable"), so the term is matched case-folded and
# whitespace-collapsed. A term outside this set fails the fetch: the applicability is what says
# whether a panel uses a criterion at all, and a value nothing encodes cannot be dropped or defaulted.
_APPLICABILITY: Mapping[str, cspec_pb2.Applicability] = {
    'applicable': cspec_pb2.APPLICABILITY_APPLICABLE,
    'not applicable': cspec_pb2.APPLICABILITY_NOT_APPLICABLE,
    'not applicable for this vcep': cspec_pb2.APPLICABILITY_NOT_APPLICABLE,
    'applicable with vcep specification': cspec_pb2.APPLICABILITY_APPLICABLE_WITH_VCEP_SPECIFICATION,
    'applicable as originally described': cspec_pb2.APPLICABILITY_APPLICABLE_AS_ORIGINALLY_DESCRIBED,
}

# The registry's records carry three spellings of one free-text field and two of another. Reading
# only the correct spelling would drop a panel's note without any absence being visible, since the
# criteria are returned fully typed and carry no raw record of their own.
_ADDITIONAL_COMMENTS_KEYS = ('additionalComments', 'additonalComments', 'additioanlComments')
_SPECIFICATION_TYPE_KEYS = ('specificationType', 'specificationtype')


@dataclasses.dataclass(frozen=True)
class SourceQuery:
    """One request issued, in the shape a `Provenance` is stamped from.

    Attributes:
        source: The upstream label.
        dataset_versions: ``"<id> <version>"`` as the sole element for a request about one document;
            empty otherwise — the registry publishes no release version of its own, and a fact read
            off a document rests on that document's version.
        query: The exact URL issued, for replay.
    """

    source: str
    dataset_versions: tuple[str, ...]
    query: str


@dataclasses.dataclass(frozen=True)
class CspecResult:
    """A gene's criteria specifications, with what the traversal reached and what it dropped.

    Attributes:
        specifications: One per document whose rule set names the gene, in the order the registry
            linked them. Unreduced: a gene covered by a legacy document and by the per-gene document
            carved out of it is named by both.
        coverage: Which of the three states the gene is in.
        raw: ``gene`` (the registry's HGNC record), ``specifications`` (each returned document's own
            record, which the lifecycle status was derived from), ``candidate_specifications`` (every
            candidate and whether its rule set named the gene) and ``rule_sets`` (the rule-set records
            whole, combining rules included).
        queries: One per request issued, for the caller to stamp as provenance.
    """

    specifications: list[cspec_pb2.VcepSpecification]
    coverage: cspec_pb2.SpecificationCoverage
    raw: dict[str, object]
    queries: list[SourceQuery]


def _entity_url(entity_type: str, identifier: str) -> str:
    """The registry URL for one entity, the identifier percent-encoded.

    The gene symbol is caller-supplied and reaches a path segment. Unencoded, a `#` truncates the
    path client-side and the registry answers about the prefix — so a mistyped symbol would come back
    as another gene's record, fail the rule-set filter, and read as "no panel has specified this gene".
    """
    return _ENTITY_URL.format(entity_type=entity_type, identifier=urllib.parse.quote(identifier, safe=''))


async def _fetch(url: str, *, http_client: httpx2.AsyncClient) -> httpx2.Response:
    return await http_client.get(url, params=_DETAIL)


def _refusal(response: httpx2.Response) -> str:
    """The registry's own explanation of a failure, from whichever envelope it used.

    The store answers a missing entity under ``status.msg``; its router answers an unparseable path
    under ``errMsg``, in a body carrying no ``status`` at all.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()
    if not isinstance(body, dict):
        return response.text.strip()
    status = body.get('status')
    if isinstance(status, dict) and isinstance(message := status.get('msg'), str):
        return message
    return message if isinstance(message := body.get('errMsg'), str) else response.text.strip()


def _data(response: httpx2.Response, *, subject: str, caller_supplied_id: bool = False) -> dict[str, object]:
    """The response's ``data`` object, failing an error status and a shape that carries none.

    Only the gene lookup asks about an identifier the caller chose, so only there is a 4xx a verdict
    on the request. On the document and rule-set hops the identifier came from the registry's own
    links, so a 4xx is the registry contradicting itself: it surfaces as an uncharacterised fault
    (retryable) rather than as INVALID_ARGUMENT, which would tell the caller its gene symbol was
    wrong and stop the guest retrying.

    Args:
        response: The registry's response.
        subject: What was asked for, for the message.
        caller_supplied_id: Whether the identifier in the URL came from the request.

    Raises:
        errors.InvalidRequestError: On a non-429 4xx for a caller-supplied identifier.
        httpx2.HTTPStatusError: On a 429 or a 5xx.
        ValueError: On any other failure, and if a 2xx carries no ``data`` object — the registry
            states an outcome in the status and the payload under ``data``, so a success without one
            is a shape change.
    """
    if not response.is_success:
        # Read only on a failure: a specification document runs to hundreds of kilobytes, and the
        # detail is another whole parse of it.
        refusal = _refusal(response)
        contradicts_its_own_links = (
            not caller_supplied_id
            and response.is_client_error
            and response.status_code != httpx2.codes.TOO_MANY_REQUESTS
        )
        if contradicts_its_own_links:
            raise ValueError(f'{_SOURCE} answered {subject} with {response.status_code}: {refusal}')
        errors.raise_for_status(response, upstream=_SOURCE, subject=subject, detail=refusal)
    body = response.json()
    if not isinstance(body, dict) or not isinstance(data := body.get('data'), dict):
        raise ValueError(f'{_SOURCE} answered {subject} 2xx with no data object')
    return data


def _object(value: object, *, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f'{_SOURCE} {what} is not an object: {value!r}')
    return value


def _objects(value: object, *, what: str) -> list[dict[str, object]]:
    """A list of objects, an absent list read as empty and anything else as a shape change."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{_SOURCE} {what} is not a list: {value!r}')
    return [_object(entry, what=f'{what} entry') for entry in value]


def _text(value: object, *, what: str) -> str:
    """A string field, an absent one read as empty and anything else as a shape change."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError(f'{_SOURCE} {what} is not a string: {value!r}')
    return value


def _strings(value: object, *, what: str) -> list[str]:
    """A field the registry states either as a string or as a list of them, always as a list.

    Both shapes are live in one field (``instructionsToUse``, ``specificationType``), so a caller
    reads one shape whichever the record used.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        raise ValueError(f'{_SOURCE} {what} is neither a string nor a list: {value!r}')
    return [_text(entry, what=f'{what} entry') for entry in value]


def _first_spelling(record: Mapping[str, object], keys: Sequence[str]) -> object:
    """The first of `keys` the record carries, for a field the registry spells several ways."""
    return next((record[key] for key in keys if key in record), None)


def _stamp(value: object, *, what: str) -> timestamp_pb2.Timestamp | None:
    """An ISO-8601 instant as a `Timestamp`, or None where the record states none.

    Raises:
        ValueError: If the value is neither absent nor an instant — a date this service cannot read
            would otherwise leave a citation carrying no approval date at all.
    """
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError(f'{_SOURCE} {what} is not a string: {value!r}')
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f'{_SOURCE} {what} is not an ISO-8601 instant: {value!r}') from e
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.UTC))
    return stamp


def _applicability(term: str, *, what: str) -> cspec_pb2.Applicability:
    """The `Applicability` value a registry term encodes; UNSPECIFIED where the record states none.

    Raises:
        ValueError: If the term is one this service does not encode. Naming the accepted values,
            because the alternative is a criterion a panel excluded reading as one it uses.
    """
    if not term.strip():
        return cspec_pb2.APPLICABILITY_UNSPECIFIED
    encoded = _APPLICABILITY.get(' '.join(term.split()).casefold())
    if encoded is None:
        accepted = ', '.join(sorted(_APPLICABILITY))
        raise ValueError(f'{_SOURCE} {what} states applicability {term!r}, which is none of: {accepted}')
    return encoded


def _points(value: object, *, what: str) -> str:
    """The panel's own points for a strength, verbatim as a string.

    The registry states this field as an integer on most records and as ``"Not Applicable"`` on
    others, and it is another framework's point value either way — a string keeps both shapes and
    keeps the value from being summed into an SVCv4 tally by accident.

    Raises:
        ValueError: If it is neither.
    """
    if value is None:
        return ''
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f'{_SOURCE} {what} is neither an integer nor a string: {value!r}')
    return str(value)


def _notes(value: object, *, what: str) -> list[cspec_pb2.CriterionNote]:
    """A strength's headed note blocks: the registry states them as ``{heading: [line, …]}``."""
    if value is None:
        return []
    block = _object(value, what=what)
    return [
        cspec_pb2.CriterionNote(heading=heading, lines=_strings(lines, what=f'{what} {heading!r}'))
        for heading, lines in block.items()
    ]


def _reference(record: Mapping[str, object], *, what: str) -> cspec_pb2.SpecificationReference:
    """One cited work. A ``pmid`` reference carries a bibliography; a ``url`` one carries a link."""
    authors = _strings(record.get('auths'), what=f'{what} authors')
    return cspec_pb2.SpecificationReference(
        namespace=_text(record.get('namespace'), what=f'{what} namespace'),
        id=_text(record.get('id'), what=f'{what} id'),
        url=_text(record.get('value'), what=f'{what} url'),
        title=_text(record.get('title'), what=f'{what} title'),
        authors='; '.join(authors),
        journal=_text(record.get('source'), what=f'{what} journal'),
        year=_text(record.get('year'), what=f'{what} year'),
        doi=_text(record.get('doiStr'), what=f'{what} doi'),
    )


def _references(value: object, *, what: str) -> list[cspec_pb2.SpecificationReference]:
    return [_reference(record, what=what) for record in _objects(value, what=what)]


def _strength(record: Mapping[str, object], *, what: str) -> cspec_pb2.StrengthSpecification:
    """One rung of a criterion's strength ladder.

    Raises:
        ValueError: If the rung states no applicability. Every one of the registry's does, and an
            UNSPECIFIED rung reads as a strength the specification is silent about rather than one it
            excludes — the reading this rpc exists to prevent.
    """
    strength = _text(record.get('strength'), what=f'{what} strength')
    term = _text(record.get('applicability'), what=f'{what} {strength} applicability')
    if not term.strip():
        raise ValueError(f'{_SOURCE} {what} {strength} states no applicability')
    return cspec_pb2.StrengthSpecification(
        strength=strength,
        applicability=_applicability(term, what=f'{what} {strength}'),
        applicability_term=term,
        text=_text(record.get('text'), what=f'{what} {strength} text'),
        instructions=_strings(record.get('instructionsToUse'), what=f'{what} {strength} instructions'),
        notes=_notes(_first_spelling(record, _ADDITIONAL_COMMENTS_KEYS), what=f'{what} {strength} notes'),
        specification_types=_strings(
            _first_spelling(record, _SPECIFICATION_TYPE_KEYS), what=f'{what} {strength} specification types'
        ),
        status=_text(record.get('status'), what=f'{what} {strength} status'),
        default_points=_points(record.get('defaultPoint'), what=f'{what} {strength} points'),
    )


def _criterion(record: Mapping[str, object], *, specification: str) -> cspec_pb2.CriterionSpecification:
    code = _text(record.get('label'), what=f'{specification} criterion label')
    what = f'{specification} {code}'
    term = _text(record.get('applicability'), what=f'{what} applicability')
    return cspec_pb2.CriterionSpecification(
        code=code,
        genes=_strings(record.get('gene'), what=f'{what} genes'),
        diseases=_strings(record.get('disease'), what=f'{what} diseases'),
        applicability=_applicability(term, what=what),
        applicability_term=term,
        base_strength=_text(record.get('baseStrength'), what=f'{what} base strength'),
        default_strength=_text(record.get('defaultStrength'), what=f'{what} default strength'),
        evidence_category=_text(record.get('evidenceCategory'), what=f'{what} evidence category'),
        original_acmg_summary=_text(record.get('originalACMGSummary'), what=f'{what} ACMG summary'),
        instructions=_strings(record.get('instructionsToUse'), what=f'{what} instructions'),
        additional_comments=_text(
            _first_spelling(record, _ADDITIONAL_COMMENTS_KEYS), what=f'{what} additional comments'
        ),
        specification_types=_strings(
            _first_spelling(record, _SPECIFICATION_TYPE_KEYS), what=f'{what} specification types'
        ),
        references=_references(record.get('references'), what=f'{what} reference'),
        strengths=_strengths(record.get('strengthDescriptor'), what=what),
    )


def _strengths(value: object, *, what: str) -> list[cspec_pb2.StrengthSpecification]:
    """A criterion's strengths, ordered strongest-first on the ACMG/AMP-2015 ladder.

    The registry's own array order is not that ladder — 244 of its criteria list Supporting ahead of
    Moderate — so a caller reading a rung by position would read the wrong one on those. Reordering a
    closed published vocabulary loses nothing, and it is what makes the position readable.

    Raises:
        ValueError: If a record names a strength outside the ladder, which has no place on it and
            would otherwise sort to one end silently.
    """
    stated = [_strength(entry, what=what) for entry in _objects(value, what=f'{what} strengths')]
    rungs = [entry.strength for entry in stated]
    if sorted(rungs) != sorted(_STRENGTH_LADDER):
        raise ValueError(f'{_SOURCE} {what} states strengths {rungs}, not the ladder {list(_STRENGTH_LADDER)}')
    return sorted(stated, key=lambda entry: _STRENGTH_LADDER.index(entry.strength))


def _scopes_gene(criterion: cspec_pb2.CriterionSpecification, gene: str) -> bool:
    """Whether a criterion applies to the requested gene.

    A criterion carrying no gene at all is scoped to the whole document — which is how a single-gene
    document states every one of its criteria — so it is kept rather than filtered out.
    """
    return not criterion.genes or gene in criterion.genes


def _entities(rule_set: Mapping[str, object], *, specification: str) -> list[cspec_pb2.SpecifiedEntity]:
    """The gene x disease entities one rule set is written for.

    A gene the rule set states no disease for still yields an entity: the panel names the gene and
    its transcript, and dropping it would report the rule set as covering no gene at all.
    """
    specified: list[cspec_pb2.SpecifiedEntity] = []
    for entry in _objects(rule_set.get('genes'), what=f'{specification} rule set genes'):
        gene = _text(entry.get('gene'), what=f'{specification} rule set gene')
        transcript = _text(entry.get('preferredTranscript'), what=f'{specification} {gene} transcript')
        diseases = _objects(entry.get('diseases'), what=f'{specification} {gene} diseases')
        if not diseases:
            specified.append(cspec_pb2.SpecifiedEntity(gene=gene, preferred_transcript=transcript))
            continue
        specified += [
            cspec_pb2.SpecifiedEntity(
                gene=gene,
                preferred_transcript=transcript,
                mondo_id=_text(disease.get('preferredMondoId'), what=f'{specification} {gene} MONDO id'),
                disease_label=_text(disease.get('preferredTitle'), what=f'{specification} {gene} disease label'),
                inheritance_terms=_inheritance_terms(disease, what=f'{specification} {gene} inheritance'),
            )
            for disease in diseases
        ]
    return specified


def _inheritance_terms(disease: Mapping[str, object], *, what: str) -> list[str]:
    """Every mode the rule set states for one disease, from either shape it states them in."""
    terms = [_text(disease.get('preferredModeOfInheritance'), what=what)]
    terms += [
        _text(mode.get('modeOfInheritance'), what=what)
        for mode in _objects(disease.get('modesOfInheritance'), what=f'{what} modes')
    ]
    return list(dict.fromkeys(term for term in terms if term))


def _rule_set_genes(rule_set: Mapping[str, object], *, specification: str) -> set[str]:
    return {
        _text(entry.get('gene'), what=f'{specification} rule set gene')
        for entry in _objects(rule_set.get('genes'), what=f'{specification} rule set genes')
    }


def _flag(content: Mapping[str, object], key: str, *, specification: str) -> bool | None:
    """One of the two legacy booleans, absent read as unstated.

    Type-checked like every other field: the two decide whether a document's text still governs, so a
    flag arriving as the string "true" would silently return a superseded document to IN_FORCE.

    Raises:
        ValueError: If the flag is neither absent nor a boolean.
    """
    value = content.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f'{_SOURCE} {specification} states {key} as {value!r}, which is not a boolean')


def _status(content: Mapping[str, object], *, specification: str) -> tuple[cspec_pb2.SpecificationStatus, str]:
    """The document's lifecycle status and the registry's own current state name.

    A document is in force when its CURRENT state is Released, it is not a legacy document its
    per-gene successors have replaced, and it is not a successor that has not yet displaced one. The
    registry's own published recipe tests the state HISTORY for a Released event instead, which
    reports a document the panel has reopened (ATM, PALB2, SERPINC1 today) as a current release —
    the one misattribution this enum exists to prevent. Released-then-reopened is its own status.
    """
    states = _objects(content.get('states'), what=f'{specification} states')
    names = [_text(state.get('name'), what=f'{specification} state name') for state in states]
    current = next((name for name, state in zip(names, states, strict=True) if state.get('current')), '')
    if _RELEASED_STATE not in names:
        return cspec_pb2.SPECIFICATION_STATUS_UNRELEASED, current
    if _flag(content, 'legacyReplaced', specification=specification) is True:
        return cspec_pb2.SPECIFICATION_STATUS_REPLACED, current
    if current != _RELEASED_STATE:
        return cspec_pb2.SPECIFICATION_STATUS_RELEASED_UNDER_REVISION, current
    if _flag(content, 'legacyFullySuperseded', specification=specification) is False:
        return cspec_pb2.SPECIFICATION_STATUS_NOT_YET_EFFECTIVE, current
    return cspec_pb2.SPECIFICATION_STATUS_IN_FORCE, current


def _citation(
    content: Mapping[str, object], document: Mapping[str, object], *, specification: str
) -> cspec_pb2.SpecificationCitation:
    doi = _object(content.get('doi') or {}, what=f'{specification} doi')
    citation = cspec_pb2.SpecificationCitation(
        document_doi=_text(doi.get('docDoi'), what=f'{specification} document doi'),
        concept_doi=_text(doi.get('conceptDoi'), what=f'{specification} concept doi'),
        version=_text(content.get('version'), what=f'{specification} version'),
        release_notes=_text(content.get('releaseNotes'), what=f'{specification} release notes'),
        registry_url=_DOCUMENT_URL.format(specification=specification),
        publisher_url=_text(content.get('specificationSource'), what=f'{specification} publisher url'),
    )
    if (approved := _stamp(content.get('approvedOn'), what=f'{specification} approval date')) is not None:
        citation.approved_on.CopyFrom(approved)
    if (modified := _stamp(document.get('modified'), what=f'{specification} modification date')) is not None:
        citation.modified.CopyFrom(modified)
    return citation


def _expert_panel(document: Mapping[str, object], *, specification: str) -> tuple[str, str]:
    """The expert panel that published the document, from the organization it is linked data for.

    Raises:
        ValueError: If the registry links none, or more than one. The panel is the attribution a
            quotation carries: with none there is nothing to attribute to, and with two there is no
            ground for choosing — picking one would put a misattribution into a citation with nothing
            marking it as a choice.
    """
    linked_for = _object(document.get('ldFor') or {}, what=f'{specification} ldFor')
    panels = _objects(linked_for.get('Organization'), what=f'{specification} organizations')
    if not panels:
        raise ValueError(f'{_SOURCE} {specification} names no publishing organization, so nothing attributes a quote')
    if len(panels) > 1:
        named = ', '.join(sorted(str(panel.get('entId')) for panel in panels))
        raise ValueError(f'{_SOURCE} {specification} is published by {len(panels)} organizations ({named})')
    content = _object(panels[0].get('entContent'), what=f'{specification} organization')
    return (
        _text(content.get('title'), what=f'{specification} panel title'),
        _text(content.get('abbreviation'), what=f'{specification} panel abbreviation'),
    )


def _attachment(record: Mapping[str, object], *, specification: str) -> cspec_pb2.SpecificationAttachment:
    content = _object(record.get('entContent'), what=f'{specification} attachment')
    size = content.get('size')
    if size is not None and not isinstance(size, int):
        raise ValueError(f'{_SOURCE} {specification} attachment size is not an integer: {size!r}')
    return cspec_pb2.SpecificationAttachment(
        label=_text(content.get('fileLabel'), what=f'{specification} attachment label'),
        description=_text(content.get('description'), what=f'{specification} attachment description'),
        file_name=_text(content.get('fileName'), what=f'{specification} attachment file name'),
        media_type=_text(content.get('type'), what=f'{specification} attachment type'),
        size_bytes=size or 0,
        registry_url=_text(record.get('ldhIri'), what=f'{specification} attachment url'),
    )


@dataclasses.dataclass(frozen=True)
class _RuleSetRecord:
    """One rule set of a document, fetched for the files a document's embedded copy does not carry."""

    raw: dict[str, object]
    general_comments: str
    references: list[cspec_pb2.SpecificationReference]
    entities: list[cspec_pb2.SpecifiedEntity]
    attachments: list[cspec_pb2.SpecificationAttachment]
    query: SourceQuery


async def _fetch_rule_set(
    ldh_id: str, *, specification: str, version: str, http_client: httpx2.AsyncClient
) -> _RuleSetRecord:
    """One rule set whole: its own record plus the files linked to it.

    Raises:
        errors.InvalidRequestError: If the registry refuses the request.
        httpx2.HTTPStatusError: On a 429 or a 5xx.
        ValueError: If the record's shape is not one this adapter reads. A rule set the document
            itself linked is one the registry holds, so a miss here is a fault, never an absence.
    """
    url = _entity_url('RuleSet', ldh_id)
    document = _data(await _fetch(url, http_client=http_client), subject=f'rule set {ldh_id!r} of {specification}')
    content = _object(document.get('entContent'), what=f'{specification} rule set {ldh_id}')
    linked = _object(document.get('ld') or {}, what=f'{specification} rule set links')
    return _RuleSetRecord(
        raw=document,
        general_comments=_text(content.get('generalComments'), what=f'{specification} general comments'),
        references=_references(content.get('references'), what=f'{specification} rule set reference'),
        entities=_entities(content, specification=specification),
        attachments=[
            _attachment(record, specification=specification)
            for record in _objects(linked.get('File'), what=f'{specification} attachments')
        ],
        query=SourceQuery(source=_SOURCE, dataset_versions=(f'{specification} {version}'.strip(),), query=url),
    )


@dataclasses.dataclass(frozen=True)
class _Document:
    """One candidate document, read and filtered against the requested gene."""

    identifier: str
    specifies_gene: bool
    specification: cspec_pb2.VcepSpecification | None
    content: dict[str, object]
    rule_sets: list[dict[str, object]]
    queries: list[SourceQuery]


async def _fetch_document(identifier: str, gene: str, *, http_client: httpx2.AsyncClient) -> _Document:
    """One candidate document, its rule sets, and whether they specify the gene.

    Raises:
        errors.InvalidRequestError: If the registry refuses the request.
        httpx2.HTTPStatusError: On a 429 or a 5xx.
        ValueError: If the record's shape is not one this adapter reads. The gene's own record linked
            this document, so the registry holding none of it is a fault rather than an absence.
    """
    url = _entity_url(_SPECIFICATION_TYPE, identifier)
    document = _data(await _fetch(url, http_client=http_client), subject=f'specification {identifier!r}')
    content = _object(document.get('entContent'), what=f'{identifier} content')
    version = _text(content.get('version'), what=f'{identifier} version')
    query = SourceQuery(source=_SOURCE, dataset_versions=(f'{identifier} {version}'.strip(),), query=url)
    linked = _object(document.get('ld') or {}, what=f'{identifier} links')

    naming = [
        record
        for record in _objects(linked.get('RuleSet'), what=f'{identifier} rule sets')
        if gene
        in _rule_set_genes(_object(record.get('entContent'), what=f'{identifier} rule set'), specification=identifier)
    ]
    if not naming:
        return _Document(
            identifier=identifier,
            specifies_gene=False,
            specification=None,
            content=dict(content),
            rule_sets=[],
            queries=[query],
        )

    rule_sets = await _fetch_rule_sets(naming, identifier, version, http_client=http_client)
    return _Document(
        identifier=identifier,
        specifies_gene=True,
        specification=_specification(identifier, gene, document, content, rule_sets),
        content=dict(content),
        rule_sets=[rule_set.raw for rule_set in rule_sets],
        queries=[query, *(rule_set.query for rule_set in rule_sets)],
    )


async def _fetch_rule_sets(
    naming: Sequence[Mapping[str, object]], identifier: str, version: str, *, http_client: httpx2.AsyncClient
) -> list[_RuleSetRecord]:
    """Every rule set that names the gene, fetched concurrently for its attachments."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(
                    _fetch_rule_set(
                        _text(record.get('ldhId'), what=f'{identifier} rule set id'),
                        specification=identifier,
                        version=version,
                        http_client=http_client,
                    )
                )
                for record in naming
            ]
    except BaseExceptionGroup as failures:
        raise errors.first_failure(failures) from failures
    return [task.result() for task in tasks]


def _specification(
    identifier: str,
    gene: str,
    document: Mapping[str, object],
    content: Mapping[str, object],
    rule_sets: Sequence[_RuleSetRecord],
) -> cspec_pb2.VcepSpecification:
    linked = _object(document.get('ld') or {}, what=f'{identifier} links')
    status, state = _status(content, specification=identifier)
    panel, abbreviation = _expert_panel(document, specification=identifier)
    stated = (
        _criterion(_object(record.get('entContent'), what=f'{identifier} criterion'), specification=identifier)
        for record in _objects(linked.get('CriteriaCode'), what=f'{identifier} criteria')
    )
    criteria = [criterion for criterion in stated if _scopes_gene(criterion, gene)]
    # A draft can genuinely link none; a released document linking none is the shape an
    # under-detailed read of the registry produces behind a 200, and it would report a panel that
    # published criteria as having published none.
    if not criteria and status != cspec_pb2.SPECIFICATION_STATUS_UNRELEASED:
        raise ValueError(f'{_SOURCE} {identifier} is released and states no criterion for {gene!r}')
    return cspec_pb2.VcepSpecification(
        id=identifier,
        title=_text(content.get('title'), what=f'{identifier} title'),
        short_title=_text(content.get('shortTitle'), what=f'{identifier} short title'),
        status=status,
        state=state,
        expert_panel=panel,
        expert_panel_abbreviation=abbreviation,
        citation=_citation(content, document, specification=identifier),
        entities=[entity for rule_set in rule_sets for entity in rule_set.entities],
        general_comments='\n\n'.join(
            rule_set.general_comments for rule_set in rule_sets if rule_set.general_comments.strip()
        ),
        references=_merged_references(
            _references(content.get('references'), what=f'{identifier} reference'),
            [reference for rule_set in rule_sets for reference in rule_set.references],
        ),
        criteria=criteria,
        attachments=[attachment for rule_set in rule_sets for attachment in rule_set.attachments],
    )


def _merged_references(
    *groups: Iterable[cspec_pb2.SpecificationReference],
) -> list[cspec_pb2.SpecificationReference]:
    """The document's and its rule sets' cited works, in order, each stated once.

    Deduplicated on the whole reference rather than on its identifier: a reference the registry
    states without one would otherwise collapse every other id-less reference into it.
    """
    merged: dict[bytes, cspec_pb2.SpecificationReference] = {}
    for group in groups:
        for reference in group:
            merged.setdefault(reference.SerializeToString(deterministic=True), reference)
    return list(merged.values())


def _candidates(gene_record: Mapping[str, object], *, gene: str) -> list[str]:
    """The specification ids the gene's own record links to — candidates, not answers."""
    linked_for = _object(gene_record.get('ldFor') or {}, what=f'{gene} ldFor')
    return [
        _text(record.get('entId'), what=f'{gene} candidate id')
        for record in _objects(linked_for.get(_SPECIFICATION_TYPE), what=f'{gene} candidates')
    ]


async def fetch_criteria_specifications(gene: str, *, http_client: httpx2.AsyncClient) -> CspecResult:
    """Fetch every CSpec criteria specification whose rule set names ``gene``.

    Args:
        gene: HGNC approved symbol, as the registry's gene table spells it (case-sensitive).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The `CspecResult`: the specifications, the coverage state, the candidates the traversal
        considered, and one `SourceQuery` per request issued.

    Raises:
        errors.InvalidRequestError: If the registry refuses a request (any non-429 4xx but the gene
            lookup's 404, which is `SPECIFICATION_COVERAGE_GENE_ABSENT`).
        httpx2.HTTPStatusError: On a 429 or a 5xx.
        ValueError: If a record's shape is not one this adapter reads.
    """
    url = _entity_url('Gene', gene)
    response = await _fetch(url, http_client=http_client)
    gene_query = SourceQuery(source=_SOURCE, dataset_versions=(), query=url)
    # The table holds every HGNC gene, so a miss is a symbol the registry has no entry for — an
    # answer about the registry's HGNC snapshot, and not the finding that no panel specified the gene.
    if response.status_code == httpx2.codes.NOT_FOUND:
        return CspecResult(
            specifications=[],
            coverage=cspec_pb2.SPECIFICATION_COVERAGE_GENE_ABSENT,
            raw={'gene': None, 'specifications': {}, 'candidate_specifications': [], 'rule_sets': {}},
            queries=[gene_query],
        )
    gene_record = _data(response, subject=f'gene {gene!r}', caller_supplied_id=True)

    documents = await _fetch_documents(_candidates(gene_record, gene=gene), gene, http_client=http_client)
    specifications = [document.specification for document in documents if document.specification is not None]
    return CspecResult(
        specifications=specifications,
        coverage=(
            cspec_pb2.SPECIFICATION_COVERAGE_SPECIFIED
            if specifications
            else cspec_pb2.SPECIFICATION_COVERAGE_NO_SPECIFICATION
        ),
        raw={
            'gene': gene_record.get('entContent'),
            # The record every `status` was derived from, so the lifecycle judgement is auditable.
            'specifications': {
                document.identifier: document.content for document in documents if document.specifies_gene
            },
            # Named rather than dropped: the registry cross-links a gene to documents that do not
            # specify it, and an unexplained absence here reads as the panel having said nothing.
            'candidate_specifications': [
                {'id': document.identifier, 'specifies_gene': document.specifies_gene} for document in documents
            ],
            'rule_sets': {document.identifier: document.rule_sets for document in documents if document.specifies_gene},
        },
        queries=[gene_query, *(query for document in documents for query in document.queries)],
    )


async def _fetch_documents(candidates: Sequence[str], gene: str, *, http_client: httpx2.AsyncClient) -> list[_Document]:
    """Every candidate document, read concurrently; a failure in one cancels the rest."""
    if not candidates:
        return []
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(_fetch_document(identifier, gene, http_client=http_client))
                for identifier in candidates
            ]
    except BaseExceptionGroup as failures:
        raise errors.first_failure(failures) from failures
    return [task.result() for task in tasks]
