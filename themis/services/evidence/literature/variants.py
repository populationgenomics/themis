"""Variant-literature discovery's pure logic: what to ask LitVar2, and how to read what it answers.

No I/O: the live adapter, the fixture and the tests share one copy of the request grammar
(``RequestedVariant``), the queries a request issues (``litvar_queries``), the entity fan-out's
breadth-first selection (``round_robin``), the per-identifier verdicts on an entity's labels
(``identifier_agreement``), and the gene inventory's narrowing and ranking (``gene_inventory``).

The variant lookup answers with LitVar2's entities, unmerged. The index keys an entity on whichever
identifier its recogniser found — an rsID, a ClinGen allele id, or a bare change string under a gene
— so one variant is split across entities that share no publication, and no entity is authoritative
for the variant. Every identifier the request carries is resolved, every entity any of them reaches
is returned, and each carries the index's own labels alongside a per-identifier verdict on how those
labels line up with the request (``IdentifierAgreement``). A disagreement is reported, never raised:
the service cannot tell a wrong request from an upstream mislabelling.

Identifiers are compared on a normalised key, never as written: a CAID's leading zeros, an rsID's
``rs`` prefix, and a protein change's three-letter residues and synonymous spelling all vary between
sources without naming a different thing (``caid_key``, ``rsid_key``, ``protein_change_key``).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

from themis.services.evidence import errors
from themis.services.evidence.upstreams import litvar


class Agreement(enum.Enum):
    """How one of an entity's labels lines up with the identifier the request supplied for it.

    A verdict, never a fault: ``DIFFERS`` says the index labelled the entity otherwise, and nothing
    about which of the two is wrong. ``UNCOMPARED`` and ``UNSTATED`` are the two ways a comparison
    does not happen — the request named no identifier of the kind, and the entity states nothing
    comparable.
    """

    UNCOMPARED = enum.auto()
    AGREES = enum.auto()
    DIFFERS = enum.auto()
    UNSTATED = enum.auto()


class IdentifierAgreement(NamedTuple):
    """One verdict per kind of identifier the request and the entity can both name."""

    gene: Agreement
    rsid: Agreement
    caid: Agreement
    change: Agreement


class RequestedVariant(NamedTuple):
    """The identifiers one variant lookup was asked about, as supplied.

    ``entity_id`` names a LitVar2 entity outright and skips resolution; the rest resolve through
    autocomplete and are compared against every entity that comes back, ``entity_id``'s included.
    Build one through ``of``, which is where the identifier grammars are enforced.
    """

    gene: str
    hgvs_c: str
    protein_change: str
    rsid: str
    caid: str
    entity_id: str

    @classmethod
    def of(
        cls, *, gene: str, hgvs_c: str, protein_change: str, rsid: str, caid: str, entity_id: str
    ) -> RequestedVariant:
        """The identifiers, whitespace-trimmed, with the two key-shaped ones checked (fail-loud).

        A key of the wrong shape reaches no entity and comes back as an empty result, which reads as
        a fact about the index rather than as the typo it is, so it is refused instead.

        Raises:
            ValueError: ``rsid`` or ``caid`` is not of its form, or nothing here reaches an entity.
        """
        requested = cls(
            gene=gene.strip(),
            hgvs_c=hgvs_c.strip(),
            protein_change=protein_change.strip(),
            rsid=rsid.strip(),
            caid=caid.strip(),
            entity_id=entity_id.strip(),
        )
        # Echoes are clipped: the message lands in a gRPC trailer, and an over-long value would
        # push the diagnosis past the cut.
        if requested.rsid and not _RSID.match(requested.rsid):
            raise ValueError(f'rsid {errors.clipped(rsid, 80)!r} is not a dbSNP rsID ("rs" and digits)')
        if requested.caid and not _CAID.match(requested.caid):
            raise ValueError(f'caid {errors.clipped(caid, 80)!r} is not a ClinGen allele id ("CA" and digits)')
        if not (
            requested.entity_id
            or requested.rsid
            or requested.caid
            or (requested.gene and (requested.hgvs_c or requested.protein_change))
        ):
            raise ValueError(
                'no identifier to resolve from: supply entity_id, rsid, caid, or gene with hgvs_c or protein_change'
            )
        return requested


class VariantEntity(NamedTuple):
    """One resolved entity: the index's account of it, the request's verdict on that, its PMIDs.

    ``total_records`` is what the index links to the entity, so it exceeds ``len(pmids)`` whenever
    the listed PMIDs are a top-ranked prefix.
    """

    labels: litvar.EntityLabels
    agreement: IdentifierAgreement
    total_records: int
    pmids: tuple[str, ...]


class VariantCensus(NamedTuple):
    """A variant lookup's entities, and how many candidates the identifiers reached.

    ``total_entities`` above ``len(entities)`` means the fan-out ceiling cut the set, so the index
    holds candidates this response does not name.
    """

    entities: tuple[VariantEntity, ...]
    total_entities: int


class GeneEntities(NamedTuple):
    """A gene's inventory rows with the census that says whether they are the whole of what matched."""

    entities: tuple[litvar.ListedEntity, ...]
    total_in_gene: int
    total_matched: int


# The residue names HGVS admits, three-letter to one-letter, so the two spellings of one change
# compare equal. `Ter` and `X` are the stop codon, which HGVS writes `*`.
_RESIDUES = {
    'ala': 'A', 'arg': 'R', 'asn': 'N', 'asp': 'D', 'cys': 'C', 'gln': 'Q', 'glu': 'E', 'gly': 'G',
    'his': 'H', 'ile': 'I', 'leu': 'L', 'lys': 'K', 'met': 'M', 'phe': 'F', 'pro': 'P', 'sec': 'U',
    'ser': 'S', 'thr': 'T', 'trp': 'W', 'tyr': 'Y', 'val': 'V', 'pyl': 'O', 'ter': '*', 'x': '*',
}  # fmt: skip
_CAID = re.compile(r'\ACA0*(\d+)\Z', re.IGNORECASE)
_RSID = re.compile(r'\A(?:rs)?(\d+)\Z', re.IGNORECASE)
# A single-residue substitution: reference residue, codon number, and the residue it becomes — the
# only protein-change form whose spellings are reconcilable without parsing HGVS in full.
_SUBSTITUTION = re.compile(r'\A([A-Za-z]{1,3})(\d+)([A-Za-z]{1,3}|\*|=)\Z')


def caid_key(caid: str) -> str:
    """A ClinGen allele id in the one form comparisons use: ``CA`` and its digits, unpadded.

    Sources disagree on zero-padding — ``CA000123`` and ``CA123`` are one id — and nothing else about
    the id varies. A string that is not ``CA`` followed by digits is compared as written, upper-cased.
    """
    match = _CAID.match(caid.strip())
    return f'CA{match.group(1)}' if match else caid.strip().upper()


def rsid_key(rsid: str) -> str:
    """A dbSNP rsID in the one form comparisons use: its digits, without the ``rs`` prefix."""
    match = _RSID.match(rsid.strip())
    return match.group(1) if match else rsid.strip().casefold()


def protein_change_key(change: str) -> str:
    """A protein change in the one form comparisons use: one-letter residues, synonymous as ``=``.

    ``p.Arg100=``, ``p.Arg100Arg``, ``p.R100=`` and ``p.R100R`` are one change written four ways, and
    sources pick freely among them. A form outside ``_SUBSTITUTION`` — a frameshift, an indel, an
    extension — is left as written, upper-cased with its parens and ``p.`` stripped, and comparing
    two such forms is left to ``protein_agreement`` rather than decided here.
    """
    bare = bare_change(change)
    match = _SUBSTITUTION.match(bare)
    if match is None:
        return bare.upper()
    reference, position, substituted = (part.casefold() for part in match.groups())
    reference = _RESIDUES.get(reference, reference).upper()
    substituted = '=' if substituted == '=' else _RESIDUES.get(substituted, substituted).upper()
    return f'{reference}{position}{"=" if substituted == reference else substituted}'


def coding_change_key(change: str) -> str:
    """A coding-DNA change in the one form comparisons use: transcript-stripped and upper-cased."""
    return bare_change(change).upper()


def bare_change(change: str) -> str:
    """Strip a transcript prefix, wrapping parens, and a leading ``p.`` for a LitVar2 query.

    LitVar2 autocomplete matches the bare change (``A100V``, ``c.299C>T``) more reliably than a
    fully-qualified HGVS form, so drop any ``transcript:`` prefix (the part before the first colon),
    then any wrapping parens and a leading ``p.``. HGVS admits the parens on either side of the
    ``p.``, so both are stripped either way round. Returns ``''`` for an empty or prefix-only input.
    """
    tail = change.split(':', 1)[-1].strip()
    return tail.strip('()').removeprefix('p.').strip('()').strip()


def litvar_queries(requested: RequestedVariant) -> list[str]:
    """The LitVar2 autocomplete queries for a variant, one per identifier the request carries.

    Every one of them is issued and every entity any of them reaches is kept: an identifier that
    resolves says nothing about whether the others reach the same entity, and they routinely do not.
    The order — ClinGen allele id, rsID, gene + protein change, gene + coding change — fixes only the
    order entities are reported in. A query is emitted only when its identifiers are present, so an
    absent one is skipped rather than sent blank, and the two key-shaped identifiers go out under the
    same normalisation their comparison uses, so a padded id and its unpadded twin ask one question.
    """
    queries = []
    if requested.caid:
        queries.append(caid_key(requested.caid))
    if requested.rsid:
        queries.append(f'rs{rsid_key(requested.rsid)}')
    if requested.gene:
        queries += [
            f'{requested.gene} {bare}'
            for bare in (bare_change(requested.protein_change), bare_change(requested.hgvs_c))
            if bare
        ]
    return queries


def identifier_agreement(requested: RequestedVariant, labels: litvar.EntityLabels) -> IdentifierAgreement:
    """The per-identifier verdict on how one entity's labels line up with the request."""
    return IdentifierAgreement(
        gene=_verdict(requested.gene, [gene.casefold() for gene in labels.genes], key=str.casefold),
        rsid=_verdict(requested.rsid, [rsid_key(labels.rsid)] if labels.rsid else [], key=rsid_key),
        caid=_verdict(requested.caid, [caid_key(caid) for caid in labels.caids], key=caid_key),
        change=_change_agreement(requested, labels.change),
    )


def _verdict(requested: str, stated: Sequence[str], *, key: Callable[[str], str]) -> Agreement:
    """The verdict for one identifier kind: ``requested`` against the keys the entity ``stated``.

    Args:
        requested: The identifier as the request supplied it; empty where it supplied none.
        stated: The entity's labels of that kind, already keyed; empty where it states none.
        key: The normalisation both sides are compared under.
    """
    if not requested:
        return Agreement.UNCOMPARED
    if not stated:
        return Agreement.UNSTATED
    return Agreement.AGREES if key(requested) in stated else Agreement.DIFFERS


def _change_agreement(requested: RequestedVariant, stated: str) -> Agreement:
    """The verdict on an entity's change string, compared within its own notation.

    A ``c.`` change is compared against ``hgvs_c`` and a ``p.`` one against ``protein_change``: the
    two notations name the same change through a translation this service does not do, so an entity
    stating the notation the request did not supply is UNSTATED rather than DIFFERS.
    """
    if not (requested.hgvs_c or requested.protein_change):
        return Agreement.UNCOMPARED
    tail = stated.split(':', 1)[-1].strip()
    if tail.startswith('c.') and requested.hgvs_c:
        return _verdict(requested.hgvs_c, [coding_change_key(tail)], key=coding_change_key)
    if tail.startswith('p.') and requested.protein_change:
        return protein_agreement(requested.protein_change, tail)
    return Agreement.UNSTATED


def protein_agreement(requested: str, stated: str) -> Agreement:
    """The verdict on two protein changes, DIFFERS only where both sides are reconcilable forms.

    ``protein_change_key`` reconciles the spellings of a single-residue substitution and leaves any
    other form as written. Two such forms differing as strings is therefore no evidence that they
    name different changes — ``p.Arg100LeufsTer5`` and ``p.R100Lfs*5`` are one change — so an
    unreconcilable pair is UNSTATED, the verdict that carries no evidence either way, rather than a
    DIFFERS the caller would act on.
    """
    if protein_change_key(requested) == protein_change_key(stated):
        return Agreement.AGREES
    reconcilable = _SUBSTITUTION.match(bare_change(requested)) and _SUBSTITUTION.match(bare_change(stated))
    return Agreement.DIFFERS if reconcilable else Agreement.UNSTATED


def round_robin(ranked: Sequence[Sequence[str]], limit: int) -> list[str]:
    """Distinct values, one taken from each ranked list per round, until ``limit``.

    Breadth first: every list is represented before any is exhausted, so one that resolved last, or
    that the upstream ranked below a list long enough to fill the limit alone, still contributes. A
    value already taken from an earlier list is skipped rather than spent twice.
    """
    selected: list[str] = []
    seen: set[str] = set()
    longest = max((len(values) for values in ranked), default=0)
    for index in range(longest):
        for values in ranked:
            if len(selected) >= limit:
                return selected
            if index >= len(values) or values[index] in seen:
                continue
            seen.add(values[index])
            selected.append(values[index])
    return selected


def gene_inventory(listed: Sequence[litvar.ListedEntity], *, contains: str, max_results: int) -> GeneEntities:
    """A gene's listing narrowed on ``contains`` and ranked most-published first, with its census.

    ``contains`` is matched against the id's last segment — the change the entity is keyed on — since
    every id also carries the index's internal gene id, which a residue number or a codon would
    match on every entity of the gene.

    The sort is stable, so entities the index states the same count for keep its own order.
    """
    needle = contains.casefold()
    matched = sorted(
        (entity for entity in listed if needle in _change_segment(entity.id)),
        key=lambda entity: -entity.total_records,
    )
    return GeneEntities(entities=tuple(matched[:max_results]), total_in_gene=len(listed), total_matched=len(matched))


def _change_segment(entity_id: str) -> str:
    """The change an entity id is keyed on, folded — its last `#`-separated segment.

    Empty for an entity keyed on an rsID or a ClinGen id alone, which states no change to narrow on.
    """
    return entity_id.rsplit('#', 1)[-1].casefold()
