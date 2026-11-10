"""Moving a change from an older version of its accession to the newest, where the two are the same around it.

A paper, or the agent, often names a superseded version: NP_001805.1 where NP_001805.4 is current. Two runs over one
paper can then choose the same change on different versions, and a change on a superseded version joins nothing
named on the current one. Updating every candidate to the newest version would be wrong where the versions differ at
the change (CTSC's NP_001805.1 and .4 differ at residue 153 alone, so p.Ile153Thr holds on one and not the other), so
a change is moved only where the newest version is the same around it: the same genomic projection, where both
versions are placed; else the same sequence across a window about the change, at the same coordinates, which
compares only a single change with a position on the coding exons or the protein. The moved change is a further
candidate, marked as an update; the agent's stays, so a stated version keeps its rank.
"""

from __future__ import annotations

import weaver

from themis.litcache import variant_fit, variant_reference
from themis.litcache.models import variants_pb2

# Positions either side of the change that must match: thirty bases, or ten residues.
_WINDOW = {'c': 30, 'p': 10}


def _span(variant: weaver.Variant) -> tuple[int, int] | None:
    """The change's first and last position, residues or c. bases.

    None for a change without a position (p.?, p.(=)), an allele of several changes, or a c. position off the
    coding exons.
    """
    if variant.members is not None:
        return None
    pos = variant.to_dict()['posedit']['pos']
    if pos is None:
        return None
    ends = (pos['start'], pos['end'] or pos['start'])
    if variant.coordinate_type == 'c' and any(e['anchor'] != 'CdsStart' or e['offset'] for e in ends):
        return None
    return ends[0]['base'], ends[1]['base']


def _located(
    accession: str, kind: str, span: tuple[int, int], references: variant_reference.References
) -> tuple[int, int] | None:
    """The span in the accession's own numbering: residues as they are, c. bases as transcript bases."""
    if kind != 'c':
        return span
    cds = references.cds_start(accession)
    if cds is None:
        return None
    # c. has no position 0: c.-1 is the base before c.1.
    start, end = (cds + c - (1 if c > 0 else 0) for c in span)
    return start, end


def _projection(variant: weaver.Variant, references: variant_reference.References) -> str | None:
    try:
        return references.mapper(variant.ac).vrs_id(variant)
    except (weaver.HGVSError, ValueError, KeyError):
        return None


def equivalent(old: str, new: str, change: str, references: variant_reference.References) -> bool:
    """Whether a change names the same thing on two versions of an accession.

    Args:
        old: The version the change is written on.
        new: Another version of the same accession.
        change: A c. or p. change, the part of an expression after ':'.
        references: The reference stores.

    Returns:
        True where both versions are placed and project the change to one genomic allele, or, failing a projection of
        either, where the sequence across a window about the change is the same on both at the same coordinates. A
        change the window cannot compare (one off the coding exons, an allele of several changes, one without a
        position) needs the projection.
    """
    try:
        before, after = weaver.parse(f'{old}:{change}'), weaver.parse(f'{new}:{change}')
    except weaver.HGVSError:
        return False
    kind = before.coordinate_type
    if kind == 'c':
        projected = _projection(before, references), _projection(after, references)
        if None not in projected:
            return projected[0] == projected[1]
    span = _span(before)
    if span is None:
        return False
    try:
        here, there = _located(old, kind, span, references), _located(new, kind, span, references)
        # A position before the first base of either version (a 5' UTR it lacks) has nothing to compare.
        if here is None or there is None or min(here[0], there[0]) < 1:
            return False
        # Only as far before the change as both versions reach: their 5' ends may differ.
        lead = min(_WINDOW[kind], here[0] - 1, there[0] - 1)
        trail = _WINDOW[kind]
        windows = (
            references.sequence(old, here[0] - lead, here[1] + trail),
            references.sequence(new, there[0] - lead, there[1] + trail),
        )
    except (KeyError, ValueError, weaver.HGVSError):
        # HGVSError: the stores' answer to a c. change on a protein accession.
        return False
    # The stores cut a range short at the 3' end: equal windows cut short end alike, but one cut into the change itself
    # (a position past the end) compares nothing.
    return windows[0] == windows[1] and len(windows[0]) >= lead + here[1] - here[0] + 1


def update(mention_set: variants_pb2.MentionSet, references: variant_reference.References) -> variants_pb2.MentionSet:
    """The mention set with each c. or p. candidate on an older version moved to the newest, where equivalent.

    Args:
        mention_set: The agent's candidates alone.
        references: The reference stores.

    Returns:
        A copy in which each mention's candidates are followed by their changes on the newest versions the stores
        hold, each a copy of its source with a version update; none is added where the newest is already a candidate.

    Raises:
        ValueError: A candidate is itself derived.
    """
    out = variants_pb2.MentionSet()
    out.CopyFrom(mention_set)
    for mention in out.mentions:
        updated: list[variants_pb2.Candidate] = []
        for candidate in mention.candidates:
            if any(r.kind in variant_fit.DERIVED_REWRITES for r in candidate.rewrites):
                raise ValueError(f"{candidate.hgvs} is derived; update the agent's candidates alone")
            accession, _, change = candidate.hgvs.partition(':')
            try:
                kind = weaver.parse(candidate.hgvs).coordinate_type
            except weaver.HGVSError:
                continue
            held = references.held_versions(accession)
            if kind not in ('c', 'p') or '.' not in accession or not held or held[-1] == accession:
                continue
            newest = f'{held[-1]}:{change}'
            if any(c.hgvs == newest for c in [*mention.candidates, *updated]) or not equivalent(
                accession, held[-1], change, references
            ):
                continue
            moved = variants_pb2.Candidate()
            moved.CopyFrom(candidate)
            moved.hgvs = newest
            moved.rewrites.add(
                kind=variants_pb2.REWRITE_KIND_VERSION_UPDATE,
                note=f'moved from {accession} to {held[-1]}, the same around the change',
                source=candidate.hgvs,
            )
            updated.append(moved)
        mention.candidates.extend(updated)
    return out
