"""Gene-disease records: assembling them from the curation tables, and resolving a disease term against them.

Two pure steps `LiveBackend.gene_disease` composes around its I/O.

`entities` turns the ClinGen and GenCC lookups into one list of `CuratedEntity`, one element per
(source, MONDO term, the source's own mode term). The mode term is kept verbatim and mapped onto
nothing: which record describes the entity being classified, its mode included, is the analyst's
call (docs/design/evidence-interfaces.md).

`resolve` answers the one question code can settle: which curated term the requested one names. The
requested term's own curations decide wherever the gene carries any, and only where it carries none
does a curated MONDO subclass descendant of that term resolve. It returns every record on the term,
under every mode, and raises rather than choose where several distinct terms sit under the requested
one, or none does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from themis.rpc import gene_disease_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import clingen_validity, gencc
from themis.svcv4 import gene_disease_validity

# MONDO's own id grammar. A curated term outside it cannot stand in a subclass relation to a
# requested MONDO term, and asking the ontology about it would report a reference-table defect as a
# fault in the caller's request.
_MONDO_ID = re.compile(r'MONDO:\d{7}')


def _mode_term(term: str, *, source: str, mondo_id: str) -> str:
    """A source's mode term, held to being stated.

    Raises:
        ValueError: If the term is blank. The record's mode is the analyst's to read off it, and a
            blank one describes no entity.
    """
    if not term.strip():
        raise ValueError(f'{source} states no mode of inheritance for its {mondo_id} assertion')
    return term


def _clingen_entity(
    result: clingen_validity.ClinGenValidityResult, curation: clingen_validity.Curation
) -> gene_disease_pb2.CuratedEntity:
    return gene_disease_pb2.CuratedEntity(
        source=result.source,
        disease_label=curation.disease_label,
        mondo_id=curation.mondo_id,
        inheritance_term=_mode_term(curation.moi, source=result.source, mondo_id=curation.mondo_id),
        validity_classification=curation.classification,
        gate_level=gene_disease_validity.gate_level(curation.classification),
    )


def _gencc_entity(result: gencc.GenCCResult, entity: gencc.Entity) -> gene_disease_pb2.CuratedEntity:
    return gene_disease_pb2.CuratedEntity(
        source=result.source,
        disease_label=entity.disease_title,
        mondo_id=entity.disease_curie,
        inheritance_term=_mode_term(entity.moi_title, source=result.source, mondo_id=entity.disease_curie),
        validity_classification=entity.classification,
        gate_level=gene_disease_validity.gate_level(entity.classification),
        submissions=[
            gene_disease_pb2.GenccSubmission(
                submitter=submission.submitter,
                validity_classification=submission.classification,
                mechanism_note=submission.mechanism_note,
            )
            for submission in entity.submissions
        ],
        mechanism_statements=[
            gene_disease_pb2.MechanismStatement(
                source=result.source, context=entity.disease_title, text=submission.mechanism_note
            )
            for submission in entity.submissions
            if submission.mechanism_note.strip()
        ],
    )


def _merged(assertions: Sequence[gene_disease_pb2.CuratedEntity]) -> gene_disease_pb2.CuratedEntity:
    """One source's assertions about one (term, mode term), as one record.

    ClinGen can state the same record more than once, as it files one row per curation, and
    the assertions reduce to its strongest classification on the published rank: the same
    aggregation under a fixed vocabulary GenCC's submitters get. Each source's own rows ride in
    ``raw`` either way.
    """
    strongest = max(assertions, key=lambda e: gene_disease_validity.rank(e.validity_classification))
    entity = gene_disease_pb2.CuratedEntity()
    entity.CopyFrom(assertions[0])
    entity.validity_classification = strongest.validity_classification
    entity.gate_level = strongest.gate_level
    del entity.submissions[:]
    del entity.mechanism_statements[:]
    for assertion in assertions:
        entity.submissions.extend(assertion.submissions)
        entity.mechanism_statements.extend(assertion.mechanism_statements)
    return entity


def _by_record(assertions: Iterable[gene_disease_pb2.CuratedEntity]) -> list[gene_disease_pb2.CuratedEntity]:
    """One source's assertions grouped onto the records they are, in first-seen order."""
    grouped: dict[tuple[str, str], list[gene_disease_pb2.CuratedEntity]] = {}
    for assertion in assertions:
        grouped.setdefault((assertion.mondo_id, assertion.inheritance_term), []).append(assertion)
    return [_merged(group) for group in grouped.values()]


def entities(
    validity: clingen_validity.ClinGenValidityResult | None, harmonised: gencc.GenCCResult | None
) -> list[gene_disease_pb2.CuratedEntity]:
    """Every curated record the two tables carry for one gene, ClinGen's first.

    Args:
        validity: The gene's ClinGen validity lookup, or None where ClinGen does not carry the gene.
        harmonised: The gene's GenCC lookup, or None where GenCC does not carry the gene.

    Returns:
        One element per (source, MONDO term, the source's mode term), reduced across nothing else.
        GenCC's entities arrive grouped on the HPO mode curie, its mode's identity, and stay as grouped.

    Raises:
        ValueError: If a source states an assertion with no mode term.
    """
    curated: list[gene_disease_pb2.CuratedEntity] = []
    if validity is not None:
        curated.extend(_by_record(_clingen_entity(validity, curation) for curation in validity.curations))
    if harmonised is not None:
        curated.extend(_gencc_entity(harmonised, entity) for entity in harmonised.entities)
    return curated


def _named(entity: gene_disease_pb2.CuratedEntity) -> str:
    return f'{entity.mondo_id} ({entity.disease_label}) under {entity.inheritance_term} [{entity.source}]'


def _listed(candidates: Iterable[gene_disease_pb2.CuratedEntity]) -> str:
    return '; '.join(sorted({_named(entity) for entity in candidates}))


def terms_needing_closure(curated: Sequence[gene_disease_pb2.CuratedEntity], mondo_id: str) -> list[str]:
    """The curated terms whose ancestry `resolve` reads, or none where it reads none.

    Args:
        curated: The gene's curated records.
        mondo_id: The requested MONDO term.

    Returns:
        The distinct MONDO terms to fetch the subclass closure for: empty where the requested term is
        itself curated, and for a curated term outside MONDO's id grammar.
    """
    if any(entity.mondo_id == mondo_id for entity in curated):
        return []
    return sorted({entity.mondo_id for entity in curated if _MONDO_ID.fullmatch(entity.mondo_id) is not None})


def _descendants(
    curated: Sequence[gene_disease_pb2.CuratedEntity], mondo_id: str, ancestors: Mapping[str, Sequence[str]]
) -> list[gene_disease_pb2.CuratedEntity]:
    """The records whose term the closure places under `mondo_id`.

    Raises:
        ValueError: If the closure carries no entry for a record whose term the ontology can be asked
            about. Reading that absence as "not a descendant" is the answer the closure was fetched
            to establish.
    """
    below = []
    for entity in curated:
        if _MONDO_ID.fullmatch(entity.mondo_id) is None:
            continue
        if entity.mondo_id not in ancestors:
            raise ValueError(f'the MONDO closure carries no ancestry for the curated term {entity.mondo_id}')
        if mondo_id in ancestors[entity.mondo_id]:
            below.append(entity)
    return below


def resolve(
    curated: Sequence[gene_disease_pb2.CuratedEntity], mondo_id: str, ancestors: Mapping[str, Sequence[str]]
) -> gene_disease_pb2.EntityResolution:
    """Which curated term the requested MONDO term names, with every record on it.

    The requested term's own curations decide wherever the gene is curated for that term. Only where
    the term is not curated at all do the curations whose term is a MONDO subclass descendant of it
    resolve, and only where exactly one such term exists. Either way the resolution carries every
    record on the term, from every source and under every mode: which of them describes the entity
    being classified is the analyst's call.

    Args:
        curated: The gene's curated records; non-empty.
        mondo_id: The requested MONDO term.
        ancestors: The MONDO subclass closure above each term `terms_needing_closure` named.

    Returns:
        The resolution, carrying every record on the resolved term.

    Raises:
        errors.UnresolvedEntityError: If the requested term is not curated and several distinct
            curated terms sit under it, or none does.
        ValueError: If `curated` is empty, or if the closure is missing a candidate's ancestry.
    """
    if not curated:
        raise ValueError("resolve takes the gene's curated records; an uncurated gene resolves nothing at all")
    on_term = [entity for entity in curated if entity.mondo_id == mondo_id]
    if on_term:
        return gene_disease_pb2.EntityResolution(
            requested_mondo_id=mondo_id,
            mondo_id=mondo_id,
            relation=gene_disease_pb2.TERM_RELATION_SAME,
            entities=on_term,
        )
    below = _descendants(curated, mondo_id, ancestors)
    terms = sorted({entity.mondo_id for entity in below})
    if len(terms) > 1:
        raise errors.UnresolvedEntityError(
            f'{len(terms)} curated terms sit under {mondo_id}: {_listed(below)}; the request names no one of them'
        )
    if not terms:
        raise errors.UnresolvedEntityError(
            f'{mondo_id} is not curated for this gene, and no curated term is a MONDO descendant of it; the gene is '
            f'curated for {_listed(curated)}. The lookup is open, not the nearest level'
        )
    return gene_disease_pb2.EntityResolution(
        requested_mondo_id=mondo_id,
        mondo_id=terms[0],
        relation=gene_disease_pb2.TERM_RELATION_DESCENDANT,
        entities=below,
    )
