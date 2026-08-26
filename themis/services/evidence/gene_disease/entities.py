"""Gene-disease entities: assembling them from the curation tables, and resolving one against them.

Two pure steps `LiveBackend.gene_disease` composes around its I/O.

`entities` turns the ClinGen and GenCC lookups into one list of `CuratedEntity`, one element per
(source, MONDO term, harmonised inheritance) — the key SM18 states the validity gate against.

`resolve` answers the one question code can settle: whether the entity the analyst named is among
them. It matches on the requested term's own curations wherever the gene carries any, and only where
it carries none does a curated MONDO subclass descendant of that term resolve. It raises rather than
choose where the key selects several entities or none.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from themis.rpc import gene_disease_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import clingen_validity, gencc
from themis.svcv4 import gene_disease_validity

# ClinGen's MOI codes and GenCC's HPO moi_curie terms onto the one harmonised mode; both sets are the
# whole vocabulary their published tables use. The X-linked spellings fold together: ClinGen curates a
# single XL, and the SVCv4 rules downstream (SM3's DAFT, SM7's POP_HMZ) take one X-linked mode.
_CLINGEN_MOI: Mapping[str, gene_disease_pb2.Inheritance] = {
    'AD': gene_disease_pb2.INHERITANCE_AUTOSOMAL_DOMINANT,
    'AR': gene_disease_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE,
    'XL': gene_disease_pb2.INHERITANCE_X_LINKED,
    'SD': gene_disease_pb2.INHERITANCE_SEMIDOMINANT,
    'MT': gene_disease_pb2.INHERITANCE_MITOCHONDRIAL,
    'UD': gene_disease_pb2.INHERITANCE_UNDETERMINED,
}
_GENCC_MOI: Mapping[str, gene_disease_pb2.Inheritance] = {
    'HP:0000006': gene_disease_pb2.INHERITANCE_AUTOSOMAL_DOMINANT,
    'HP:0000007': gene_disease_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE,
    'HP:0001417': gene_disease_pb2.INHERITANCE_X_LINKED,
    'HP:0001419': gene_disease_pb2.INHERITANCE_X_LINKED,  # X-linked recessive
    'HP:0001423': gene_disease_pb2.INHERITANCE_X_LINKED,  # X-linked dominant
    'HP:0001450': gene_disease_pb2.INHERITANCE_Y_LINKED,
    'HP:0001427': gene_disease_pb2.INHERITANCE_MITOCHONDRIAL,
    'HP:0032113': gene_disease_pb2.INHERITANCE_SEMIDOMINANT,
    'HP:0000005': gene_disease_pb2.INHERITANCE_UNDETERMINED,  # GenCC's "Unknown"
}

# MONDO's own id grammar. A curated term outside it cannot stand in a subclass relation to a
# requested MONDO term, and asking the ontology about it would report a reference-table defect as a
# fault in the caller's request.
_MONDO_ID = re.compile(r'MONDO:\d{7}')


def _inheritance(
    mode: str, vocabulary: Mapping[str, gene_disease_pb2.Inheritance], source: str
) -> gene_disease_pb2.Inheritance:
    """The harmonised mode a source's own term names.

    Raises:
        ValueError: If the source's vocabulary has moved. The mode is half of what identifies an
            entity, so a value nothing harmonises can only be matched, dropped or defaulted wrongly.
    """
    try:
        return vocabulary[mode]
    except KeyError:
        raise ValueError(
            f'{source} states mode of inheritance {mode!r}, which no harmonised mode covers; the known '
            f'ones are {sorted(vocabulary)}'
        ) from None


def _clingen_entity(
    result: clingen_validity.ClinGenValidityResult, curation: clingen_validity.Curation
) -> gene_disease_pb2.CuratedEntity:
    return gene_disease_pb2.CuratedEntity(
        source=result.source,
        disease_label=curation.disease_label,
        mondo_id=curation.mondo_id,
        inheritance=_inheritance(curation.moi, _CLINGEN_MOI, result.source),
        inheritance_term=curation.moi,
        validity_classification=curation.classification,
        gate_level=gene_disease_validity.gate_level(curation.classification),
    )


def _gencc_entity(result: gencc.GenCCResult, entity: gencc.Entity) -> gene_disease_pb2.CuratedEntity:
    return gene_disease_pb2.CuratedEntity(
        source=result.source,
        disease_label=entity.disease_title,
        mondo_id=entity.disease_curie,
        inheritance=_inheritance(entity.moi_curie, _GENCC_MOI, result.source),
        inheritance_term=entity.moi_title,
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
    """One source's assertions about one entity, as that entity.

    A source can state the same (term, harmonised mode) more than once — GenCC keys its groups on the
    HPO term, so the X-linked spellings arrive separately — and the entity is the same one. They
    reduce to its strongest classification on the published rank, the same aggregation under a fixed
    vocabulary GenCC's submitters get; each source's own rows ride in ``raw`` either way.
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


def _by_entity(assertions: Iterable[gene_disease_pb2.CuratedEntity]) -> list[gene_disease_pb2.CuratedEntity]:
    """One source's assertions grouped onto the entities they are about, in first-seen order."""
    grouped: dict[tuple[str, gene_disease_pb2.Inheritance], list[gene_disease_pb2.CuratedEntity]] = {}
    for assertion in assertions:
        grouped.setdefault(_key(assertion), []).append(assertion)
    return [_merged(group) for group in grouped.values()]


def entities(
    validity: clingen_validity.ClinGenValidityResult | None, harmonised: gencc.GenCCResult | None
) -> list[gene_disease_pb2.CuratedEntity]:
    """Every curated entity the two tables carry for one gene, ClinGen's first.

    Args:
        validity: The gene's ClinGen validity lookup, or None where ClinGen does not carry the gene.
        harmonised: The gene's GenCC lookup, or None where GenCC does not carry the gene.

    Returns:
        One element per (source, MONDO term, harmonised inheritance), reduced across nothing else.

    Raises:
        ValueError: If a source states a mode of inheritance no harmonised mode covers.
    """
    curated: list[gene_disease_pb2.CuratedEntity] = []
    if validity is not None:
        curated.extend(_by_entity(_clingen_entity(validity, curation) for curation in validity.curations))
    if harmonised is not None:
        curated.extend(_by_entity(_gencc_entity(harmonised, entity) for entity in harmonised.entities))
    return curated


def _key(entity: gene_disease_pb2.CuratedEntity) -> tuple[str, gene_disease_pb2.Inheritance]:
    return entity.mondo_id, entity.inheritance


def _named(entity: gene_disease_pb2.CuratedEntity) -> str:
    return f'{entity.mondo_id} ({entity.disease_label}) under {entity.inheritance_term} [{entity.source}]'


def _listed(candidates: Iterable[gene_disease_pb2.CuratedEntity]) -> str:
    return '; '.join(sorted({_named(entity) for entity in candidates}))


def _under_requested_mode(
    candidates: Sequence[gene_disease_pb2.CuratedEntity], inheritance: gene_disease_pb2.Inheritance
) -> list[gene_disease_pb2.CuratedEntity]:
    """The candidates curated under the requested mode; every candidate when none was requested."""
    if inheritance == gene_disease_pb2.INHERITANCE_UNSPECIFIED:
        return list(candidates)
    return [entity for entity in candidates if entity.inheritance == inheritance]


def terms_needing_closure(
    curated: Sequence[gene_disease_pb2.CuratedEntity], mondo_id: str, inheritance: gene_disease_pb2.Inheritance
) -> list[str]:
    """The curated terms whose ancestry `resolve` reads, or none where it reads none.

    Args:
        curated: The gene's curated entities.
        mondo_id: The requested MONDO term.
        inheritance: The requested mode, or INHERITANCE_UNSPECIFIED to narrow by term alone.

    Returns:
        The distinct MONDO terms to fetch the subclass closure for: empty where the requested term is
        itself curated, where nothing is curated under the requested mode, and for a curated term
        outside MONDO's id grammar.
    """
    if any(entity.mondo_id == mondo_id for entity in curated):
        return []
    return sorted(
        {
            entity.mondo_id
            for entity in _under_requested_mode(curated, inheritance)
            if _MONDO_ID.fullmatch(entity.mondo_id) is not None
        }
    )


def _resolution(
    matched: Sequence[gene_disease_pb2.CuratedEntity],
    *,
    requested_mondo_id: str,
    requested_inheritance: gene_disease_pb2.Inheritance,
    relation: gene_disease_pb2.TermRelation,
) -> gene_disease_pb2.EntityResolution:
    return gene_disease_pb2.EntityResolution(
        requested_mondo_id=requested_mondo_id,
        requested_inheritance=requested_inheritance,
        mondo_id=matched[0].mondo_id,
        inheritance=matched[0].inheritance,
        relation=relation,
        entities=matched,
    )


def _on_requested_term(
    curated: Sequence[gene_disease_pb2.CuratedEntity], mondo_id: str, inheritance: gene_disease_pb2.Inheritance
) -> gene_disease_pb2.EntityResolution:
    """Resolve against the curations of the requested term itself.

    Raises:
        errors.UnresolvedEntityError: If the term is curated only under other modes, or under several
            and the request named none.
    """
    on_term = [entity for entity in curated if entity.mondo_id == mondo_id]
    matched = _under_requested_mode(on_term, inheritance)
    if not matched:
        raise errors.UnresolvedEntityError(
            f'{mondo_id} is curated under {sorted({e.inheritance_term for e in on_term})}, and the request states '
            f'another mode; the curated entity is not the one asked about. The gene is curated for {_listed(curated)}'
        )
    if len({entity.inheritance for entity in matched}) > 1:
        raise errors.UnresolvedEntityError(
            f"{mondo_id} is curated under {sorted({e.inheritance_term for e in matched})}; state the entity's "
            f'inheritance, which is half of what identifies it'
        )
    return _resolution(
        matched,
        requested_mondo_id=mondo_id,
        requested_inheritance=inheritance,
        relation=gene_disease_pb2.TERM_RELATION_SAME,
    )


def _descendants(
    candidates: Sequence[gene_disease_pb2.CuratedEntity], mondo_id: str, ancestors: Mapping[str, Sequence[str]]
) -> list[gene_disease_pb2.CuratedEntity]:
    """The candidates whose term the closure places under `mondo_id`.

    Raises:
        ValueError: If the closure carries no entry for a candidate whose term the ontology can be
            asked about. Reading that absence as "not a descendant" is the answer the closure was
            fetched to establish.
    """
    below = []
    for entity in candidates:
        if _MONDO_ID.fullmatch(entity.mondo_id) is None:
            continue
        if entity.mondo_id not in ancestors:
            raise ValueError(f'the MONDO closure carries no ancestry for the curated term {entity.mondo_id}')
        if mondo_id in ancestors[entity.mondo_id]:
            below.append(entity)
    return below


def _below_requested_term(
    curated: Sequence[gene_disease_pb2.CuratedEntity],
    mondo_id: str,
    inheritance: gene_disease_pb2.Inheritance,
    ancestors: Mapping[str, Sequence[str]],
) -> gene_disease_pb2.EntityResolution:
    """Resolve against the curations whose term is a MONDO subclass descendant of the requested one.

    Raises:
        errors.UnresolvedEntityError: If nothing is curated under the requested mode, if several
            distinct entities sit under the term, or if none does.
    """
    candidates = _under_requested_mode(curated, inheritance)
    if not candidates:
        raise errors.UnresolvedEntityError(
            f'the gene is curated only under {sorted({e.inheritance_term for e in curated})}, and the request states '
            f'another mode; neither {mondo_id} nor a MONDO descendant of it is curated under the requested one. The '
            f'gene is curated for {_listed(curated)}'
        )
    below = _descendants(candidates, mondo_id, ancestors)
    keys = {_key(entity) for entity in below}
    if len(keys) > 1:
        raise errors.UnresolvedEntityError(
            f'{len(keys)} curated entities sit under {mondo_id}: {_listed(below)}; the request names no one of them'
        )
    if not keys:
        raise errors.UnresolvedEntityError(
            f'{mondo_id} is not curated for this gene, and under the requested inheritance no curated term is a MONDO '
            f'descendant of it; the gene is curated for {_listed(curated)}. The lookup is open, not the nearest level'
        )
    return _resolution(
        below,
        requested_mondo_id=mondo_id,
        requested_inheritance=inheritance,
        relation=gene_disease_pb2.TERM_RELATION_DESCENDANT,
    )


def resolve(
    curated: Sequence[gene_disease_pb2.CuratedEntity],
    mondo_id: str,
    inheritance: gene_disease_pb2.Inheritance,
    ancestors: Mapping[str, Sequence[str]],
) -> gene_disease_pb2.EntityResolution:
    """Which curated entity the requested MONDO term names.

    The requested term's own curations decide wherever the gene is curated for that term, whatever
    mode they are under: a term curated under another mode is a conflict with the entity the caller
    states, not an occasion to substitute a subtype. Only where the term is not curated at all do the
    curations whose term is a MONDO subclass descendant of it resolve. Both are matched under the
    requested mode, which is half of the entity's identity — one gene's MDEs can differ in mode alone
    (SM21).

    Args:
        curated: The gene's curated entities; non-empty.
        mondo_id: The requested MONDO term.
        inheritance: The requested mode, or INHERITANCE_UNSPECIFIED to narrow by term alone.
        ancestors: The MONDO subclass closure above each term `terms_needing_closure` named.

    Returns:
        The resolution, carrying every entity that names it (one per source that curates it).

    Raises:
        errors.UnresolvedEntityError: If the request selects several of the gene's curated entities,
            or none of them.
        ValueError: If `curated` is empty, or if the closure is missing a candidate's ancestry.
    """
    if not curated:
        raise ValueError("resolve takes the gene's curated entities; an uncurated gene resolves nothing at all")
    if any(entity.mondo_id == mondo_id for entity in curated):
        return _on_requested_term(curated, mondo_id, inheritance)
    return _below_requested_term(curated, mondo_id, inheritance, ancestors)
