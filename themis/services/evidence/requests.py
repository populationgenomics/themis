"""Request preconditions the evidence interfaces hold their callers to, shared across interfaces.

They live beside the interfaces rather than inside a backend so they bind the fixture and live
adapters alike — the protos state them unconditionally. `gnomad` and `splice` need theirs most:
their upstreams answer an unparsable id with a *success* carrying an absence, and for those two an
absence is the evidence, so an unchecked field turns a caller's typo into a finding.

Each takes the rpc's name for the message, since one function serves several rpcs.
"""

from __future__ import annotations

import re

from themis.services.evidence import errors

# Coding accessions only: the exon table is reported in c. coordinates and the splice outcome is
# translated from the CDS, neither of which an NR_ transcript has.
_TRANSCRIPT_ACCESSION = re.compile(r'NM_\d{1,15}\.\d{1,5}')
_GENOME_BUILDS = ('GRCh38', 'GRCh37')

# A MONDO curie, whose local ids are seven digits throughout the ontology. A wider pattern would let
# a mistyped id reach the curation tables and come back as an entity mismatch rather than as a typo.
_MONDO_ID = re.compile(r'MONDO:\d{7}')

# ClinVar's variation accession, zero-padded to nine digits. No version suffix: a version names one
# revision of the record, and these rpcs answer about the variation as ClinVar currently holds it.
_VCV_ACCESSION = re.compile(r'VCV\d{9}')

# An HGNC id, `HGNC:nnnn`. Case-insensitive, unlike the positional id below: the reference tables key
# on the id upper-cased, so `hgnc:1100` resolves and refusing it would reject a request they answer.
_HGNC_ID = re.compile(r'HGNC:\d{1,7}', re.IGNORECASE)

# A gnomAD/VCF positional id, `chrom-pos-ref-alt`, in the one casing both upstreams take — which is
# also what `Resolve` emits. Deliberately NOT case-insensitive: gnomAD tolerates `CHR1-…-g-a` where
# the Broad services parse none of it, so a tolerant check would pass `Splice` a variant that comes
# back "unscorable". The chromosome is the real set rather than any one or two digits, because the
# Broad services answer a contig that does not exist ("chr0", "chr23", "chr99") with the same "did
# not return any scores" they use for a position they genuinely cannot score — so a nonexistent
# contig would otherwise read as an SPL_PRD finding. The allele runs are unbounded here: a large
# indel spells a long id and that is a well-formed allele, not a malformed field.
#
# `M`/`MT` are in the set and are real, but the SpliceAI host 503s on both, deterministically. That
# leaves `Splice` retrying a 5xx and ending at UNKNOWN — correct handling of a 5xx, and not ours.
_POSITIONAL_ID = re.compile(r'(?:chr)?(?:[1-9]|1\d|2[0-2]|MT|[XYM])-\d{1,12}-[ACGTN]+-[ACGTN]+')

# The longest positional id these rpcs carry. A TRANSPORT bound, not a shape one: the Broad splice
# services take the variant in a GET query string, and their hosts answer a request line past ~8 kB
# with a bare 400 that names nothing. Measured against them: a 4103-character URL is scored, an
# 8003-character one is refused. Nothing under this is refused for its length — a 1 kb deletion's
# 1019-character id is well-formed, gnomAD answers it and SpliceAI scores it — and the two refusals
# are separate messages, because "not that shape" sends a caller looking for a typo in an id that has
# none. gnomAD carries its id in a POST body and has no such limit; one bound serves both rpcs
# because no allele gnomAD's short-variant callset holds comes near it.
_MAX_POSITIONAL_ID = 4000

# How much of a rejected id a message repeats. An error message is clipped whole to fit a gRPC
# trailer, so echoing an over-long id would push the diagnosis past the cut and lose it.
_ECHOED_ID = 80


def require_genome_build(rpc: str, genome_build: str) -> None:
    """Reject a genome build here rather than after an upstream round-trip.

    Raises:
        errors.InvalidRequestError: If `genome_build` is not one the upstreams serve.
    """
    if genome_build not in _GENOME_BUILDS:
        raise errors.InvalidRequestError(
            f'{rpc} takes genome_build {" or ".join(_GENOME_BUILDS)}; got {genome_build!r}'
        )


def require_hgnc_id(rpc: str, hgnc_id: str) -> None:
    """Hold the gene to the HGNC id the reference tables are keyed on.

    A symbol, or an id carrying padding, keys none of the tables — unchecked it comes back as "no
    source carries this gene", a statement about the tables rather than about the request that
    produced it. Padding is refused rather than stripped: the id is a table key, not text sent to an
    upstream, so there is nothing to clean it up for.

    An unset field is the separate message, because it is a different mistake with a different
    remedy: the caller has an id to supply and did not.

    Raises:
        errors.InvalidRequestError: If `hgnc_id` is empty, or is not an HGNC id.
    """
    if not hgnc_id:
        raise errors.InvalidRequestError(
            f'{rpc} carries no hgnc_id; the reference tables key on HGNC id, which Variant.Normalize '
            'supplies on the NormalizeResponse'
        )
    if _HGNC_ID.fullmatch(hgnc_id) is None:
        raise errors.InvalidRequestError(f'{rpc} takes the gene as an HGNC id ("HGNC:" and digits); got {hgnc_id!r}')


def require_mondo_id(rpc: str, mondo_id: str) -> None:
    """Hold a disease entity to a MONDO curie, so a mistyped one is not read as a curation gap.

    A value that is not a curie at all would resolve against no curation and come back as "the gene is
    curated for other entities than yours" — a statement about the curations rather than about the
    request that produced it. A well-formed curie the ontology does not hold is not caught here and
    answers that way; the rpc asks MONDO about curated terms, not requested ones.

    Raises:
        errors.InvalidRequestError: If `mondo_id` is not a MONDO curie.
    """
    if _MONDO_ID.fullmatch(mondo_id) is None:
        raise errors.InvalidRequestError(
            f'{rpc} takes the disease entity as a MONDO curie ("MONDO:" and seven digits); got {mondo_id!r}'
        )


def require_vcv_accession(rpc: str, vcv: str) -> None:
    """Hold a ClinVar variation accession to the zero-padded form efetch resolves.

    A bare numeric UID is what forces the check: efetch takes one with a 200 carrying an empty
    `<set/>`, so unchecked it reads back as ClinVar holding no archive for a variation whose archive
    it holds — an absence manufactured out of a padding difference.

    Raises:
        errors.InvalidRequestError: If `vcv` is not `VCV` and nine digits.
    """
    if _VCV_ACCESSION.fullmatch(vcv) is None:
        raise errors.InvalidRequestError(
            f'{rpc} takes vcv as a zero-padded ClinVar variation accession, e.g. VCV000704508, as '
            f"Variant.Normalize's clinvar_variations states it; got {vcv!r}"
        )


def require_transcript(rpc: str, transcript: str) -> None:
    """Reject anything but the bare versioned RefSeq coding accession the exon-table rpcs take.

    Raises:
        errors.InvalidRequestError: If `transcript` is not one — an unversioned accession would name
            an exon structure the caller cannot reproduce, and an Ensembl one has no RefSeq record.
    """
    if _TRANSCRIPT_ACCESSION.fullmatch(transcript) is None:
        raise errors.InvalidRequestError(
            f'{rpc} takes a versioned RefSeq coding transcript accession, e.g. NM_001042492.3; got {transcript!r}'
        )


def require_positional_id(rpc: str, field: str, variant: str) -> None:
    """Hold a `chrom-pos-ref-alt` field to that shape, and to what the transport can carry.

    Both upstreams behind these rpcs answer a malformed id with a *success* carrying an absence —
    gnomAD with a null variant, the Broad services with an unscored payload — which the adapters
    then report as NOT_FOUND. For `Gnomad` and `Splice` that absence IS the evidence (PRODUCT §6:
    rarity, and an unscorable position), so a typo would otherwise be scored as a finding about the
    variant. The shape check is what keeps a caller's mistake from becoming evidence.

    Length is judged apart from shape and says so, because the two are different faults with
    different remedies: a well-formed id of a large indel is the service's own `Resolve` output, and
    reporting it as the wrong *kind* of identifier sends the caller to look for a typo that is not
    there.

    Raises:
        errors.InvalidRequestError: If `variant` is not `chrom-pos-ref-alt`, or is longer than the
            Broad hosts' request line carries.
    """
    echoed = errors.clipped(variant, _ECHOED_ID)
    if _POSITIONAL_ID.fullmatch(variant) is None:
        raise errors.InvalidRequestError(
            f'{rpc} takes {field} as a gnomAD-style chrom-pos-ref-alt id, e.g. 17-31232881-G-C; got {echoed!r}'
        )
    if len(variant) > _MAX_POSITIONAL_ID:
        raise errors.InvalidRequestError(
            f'{rpc} takes {field} as an id of at most {_MAX_POSITIONAL_ID} characters, and this is a '
            f'well-formed id of {len(variant)} characters ({echoed!r}). The bound is the shorter of what the '
            'two positional rpcs can carry, so one id serves either: the Broad splice hosts cut off a GET '
            'request line past ~8 kB. The allele is a multi-kilobase indel, which neither rpc reaches — the '
            'splice predictors cannot be handed the sequence, and gnomAD holds an allele that size in its '
            'structural-variant release rather than in the short-variant callset these frequencies come from.'
        )


def require_cds_range(rpc: str, start: int, end: int) -> None:
    """Hold a c. span to two coordinates in transcript order.

    Plain integer order *is* transcript order across the two representable regions: every 5'UTR
    coordinate is negative and every CDS one positive, so c.-20 < c.-1 < c.1 sorts as written.

    Raises:
        errors.InvalidRequestError: If either endpoint is 0 — c. numbering has none, so this is where
            an unset field is caught — or if the range descends.
    """
    for name, position in (('cds_start', start), ('cds_end', end)):
        if position == 0:
            raise errors.InvalidRequestError(
                f"{rpc} takes {name} as a c. coordinate (negative = 5'UTR), and c. numbering has no 0; "
                'an unset field arrives as one'
            )
    if end < start:
        raise errors.InvalidRequestError(
            f'{rpc} takes cds_start..cds_end in transcript order; got c.{start} to c.{end}'
        )


def require_gene(rpc: str, gene: str, *, purpose: str, context: str = '') -> str:
    """The HGNC symbol an rpc scopes its query by, stripped so no upstream is sent the padding.

    Raises:
        errors.InvalidRequestError: If it is empty.
    """
    stripped = gene.strip()
    if not stripped:
        detail = f'{rpc} takes an HGNC symbol {purpose}; got an empty gene'
        raise errors.InvalidRequestError(f'{detail} {context}' if context else detail)
    return stripped
