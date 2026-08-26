"""The `hgvs` module's two directions: accepting an expression, and reading coordinates back out.

`accepted_protein_hgvs` is tested here directly, since the rule under test is an equivalence in the
notation rather than a heuristic: a change wrapped in parentheses is the *predicted* rendering of the
same allele as the bare one, so both must reduce to one form. Everything else in the change is left
verbatim — the amino-acid vocabulary and the synonymous spellings (`p.Ser400Ser` for `p.Ser400=`) are
the Allele Registry's to reconcile. `accepted_hgvs` and `accepted_transcript_hgvs` are otherwise
exercised through the rpcs that hold callers to them (each interface's own servicer tests) and
through each adapter; what both have to take is tested here, since the rpcs sit in different
interfaces and neither can state the shared rule alone.

`coding_span` reads the other direction — coordinates back out of an expression an upstream wrote.
"""

from __future__ import annotations

import pytest

from themis.rpc import clinvar_pb2
from themis.services.evidence import errors, hgvs


def _triple(coordinate: hgvs.CodingCoordinate) -> tuple[int, int, int]:
    return coordinate.region, coordinate.position, coordinate.intron_offset


@pytest.mark.parametrize(
    ('expression', 'start', 'end'),
    [
        # A single-base change spans one base, so both ends are the same coordinate.
        ('NM_000267.3:c.3496G>C', (clinvar_pb2.CODING_REGION_CDS, 3496, 0), (clinvar_pb2.CODING_REGION_CDS, 3496, 0)),
        (
            'NM_000492.4:c.1521_1523delCTT',
            (clinvar_pb2.CODING_REGION_CDS, 1521, 0),
            (clinvar_pb2.CODING_REGION_CDS, 1523, 0),
        ),
        # The three regions restart their numbering, so the region is what tells these apart.
        (
            'NM_000267.3:c.-25C>T',
            (clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR, 25, 0),
            (clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR, 25, 0),
        ),
        (
            'NM_000267.3:c.*25A>G',
            (clinvar_pb2.CODING_REGION_THREE_PRIME_UTR, 25, 0),
            (clinvar_pb2.CODING_REGION_THREE_PRIME_UTR, 25, 0),
        ),
        # An intronic base is numbered off the exon it is nearest, on whichever side.
        (
            'NM_000267.3:c.3496+1G>A',
            (clinvar_pb2.CODING_REGION_CDS, 3496, 1),
            (clinvar_pb2.CODING_REGION_CDS, 3496, 1),
        ),
        (
            'NM_000267.3:c.3497-2A>G',
            (clinvar_pb2.CODING_REGION_CDS, 3497, -2),
            (clinvar_pb2.CODING_REGION_CDS, 3497, -2),
        ),
        # A UTR intron carries both markers, and the leading "-" is the region while the trailing
        # one is the offset.
        (
            'NM_000267.3:c.-33-2A>G',
            (clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR, 33, -2),
            (clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR, 33, -2),
        ),
        # A deletion crossing a splice boundary has ends in different intron positions.
        (
            'NM_000267.3:c.3496+1_3497-1del',
            (clinvar_pb2.CODING_REGION_CDS, 3496, 1),
            (clinvar_pb2.CODING_REGION_CDS, 3497, -1),
        ),
    ],
)
def test_each_hgvs_position_form_decodes_to_its_own_coordinate(
    expression: str, start: tuple[int, int, int], end: tuple[int, int, int]
) -> None:
    span = hgvs.coding_span(expression)
    assert span is not None
    assert (_triple(span.start), _triple(span.end)) == (start, end)


def test_the_transcript_the_coordinates_are_numbered_on_comes_back_with_them() -> None:
    """A gene's pool is not written against one transcript, so the accession is half the coordinate."""
    span = hgvs.coding_span('NM_001042492.3(NF1):c.3496G>C (p.Gly1166Arg)')
    assert span is not None
    assert span.transcript == 'NM_001042492.3'


@pytest.mark.parametrize(
    'expression',
    [
        'GRCh38/hg38 17q11.2(chr17:31094927-31377677)x1',  # a copy-number record, titled cytogenetically
        'NM_000267.3(NF1):c.(?_-33)_(*1_?)del',  # boundaries ClinVar states as unknown
        'NC_000017.11:g.31350290del',  # genomic coordinates, no c. rendering
        'NM_000267.3(NF1):p.Gly1166Arg',  # protein only
        'NM_000267.3(NF1):c.3496',  # a position with no change is not an expression
        'NM_000267.3(NF1):c.1234567890G>A',  # longer than any transcript: unparsed, never truncated
        '',
    ],
)
def test_an_expression_naming_no_coding_span_is_unparsed_rather_than_placed(expression: str) -> None:
    """None is a record that cannot be placed; the caller has to say so rather than drop it."""
    assert hgvs.coding_span(expression) is None


@pytest.mark.parametrize(
    'title',
    [
        # ClinVar's two renderings of a record classified over several alleles at once. Its search
        # returns them for any constituent allele, so a gene pool holds them.
        'NM_000492.4(CFTR):c.[1521_1523delCTT;3080T>C]',
        'NM_000492.4(CFTR):c.1521_1523delCTT (p.Phe508del) AND NM_000492.4(CFTR):c.2562T>G',
        'NM_000492.4(CFTR):c.1521_1523delCTT, NM_000492.4(CFTR):c.2562T>G',
    ],
)
def test_a_title_naming_several_alleles_is_unparsed_rather_than_read_as_its_first(title: str) -> None:
    """Placed at its first allele it would land confidently in one exon, and be counted there.

    That is worse than being unplaced: unplaced is reported, and a wrong exon is not.
    """
    assert hgvs.coding_span(title) is None


@pytest.mark.parametrize(
    'title',
    [
        'NM_005249.5(FOXG1):c.219GCC[5] (p.Pro80del)',
        'NM_000527.5(LDLR):c.571ATG[1]',
    ],
)
def test_short_form_repeat_notation_is_unparsed_rather_than_placed_at_its_first_base(title: str) -> None:
    """The number is the tract's start; how far the tract runs is the reference's, not the string's.

    Read as the one base it numbers, the record would be counted at a codon the repeat may only
    begin at — a placement the expression never made.
    """
    assert hgvs.coding_span(title) is None


def test_an_insertion_range_brackets_the_insertion_point() -> None:
    """HGVS writes an insertion range exclusive, so neither endpoint is a base the allele alters."""
    span = hgvs.coding_span('NM_000267.3(NF1):c.3496_3497insA')
    assert span is not None
    assert (span.start.position, span.end.position) == (3496, 3497)


def test_a_five_prime_utr_span_descends_because_the_region_numbers_towards_the_start_codon() -> None:
    """`c.-25` is upstream of `c.-1`, so `start.position <= end.position` does not hold there."""
    span = hgvs.coding_span('NM_000267.3(NF1):c.-25_-20del')
    assert span is not None
    assert span.start.position > span.end.position


def test_the_three_regions_never_collide_at_one_number() -> None:
    """`c.-25`, `c.25` and `c.*25` are three bases; a signed integer alone cannot hold all three."""
    spans = [hgvs.coding_span(f'NM_000267.3:c.{prefix}25C>T') for prefix in ('-', '', '*')]
    assert all(span is not None for span in spans)
    regions = {span.start.region for span in spans if span is not None}
    assert len(regions) == 3


@pytest.mark.parametrize(
    ('variant', 'accepted'),
    [
        ('NP_000537.3:p.Arg175His', 'NP_000537.3:p.Arg175His'),
        ('NP_000537.3:p.(Arg175His)', 'NP_000537.3:p.Arg175His'),
        ('NP_000537.3(TP53):p.(Arg175His)', 'NP_000537.3:p.Arg175His'),
        ('NP_000537.3:p.(R175H)', 'NP_000537.3:p.R175H'),  # one-letter code: a rendering, not another allele
        ('NP_000537.3:p.(Ser400=)', 'NP_000537.3:p.Ser400='),
        ('NP_000537.3:p.(Met1?)', 'NP_000537.3:p.Met1?'),
        ('NP_000537.3:p.(Ter394Argext*?)', 'NP_000537.3:p.Ter394Argext*?'),
    ],
)
def test_a_predicted_change_and_an_observed_one_name_the_same_allele(variant: str, accepted: str) -> None:
    assert hgvs.accepted_protein_hgvs('Mavedb', variant) == accepted


@pytest.mark.parametrize(
    'variant',
    [
        'NP_000537.3:p.Arg175_Gly176ins(5)',  # an unknown-length insertion: the change's own parentheses
        'NP_000537.3:p.Gly1_Ala2delins(Arg)',
        'NP_000537.3:p.(Gly1del)(Ala2del)',  # two wrappers, neither enclosing: stripping would corrupt it
    ],
)
def test_parentheses_that_are_part_of_the_change_survive(variant: str) -> None:
    """Only a pair enclosing the whole change is the predicted-consequence wrapper."""
    assert hgvs.accepted_protein_hgvs('Mavedb', variant) == variant


@pytest.mark.parametrize(
    'variant',
    [
        'p.Arg175His',  # no accession: names no sequence to read the position against
        'ENSP00000269305.4:p.Arg175His',  # the Allele Registry registers no id for an Ensembl protein
        'XP_011526312.1:p.Arg175His',  # nor for a predicted RefSeq one
        'NP_000537:p.Arg175His',  # unversioned: the registry answers it with a 500, not a verdict
        'NM_000546.6:c.524G>A',  # a coding change, for the acceptor that takes those
        'NP_000537.3:p.',
        'NP_000537.3:p.()',
        'NP_000537.3:p.(',
        'NP_000537.3',
        '',
    ],
)
def test_an_expression_that_names_no_protein_allele_is_refused(variant: str) -> None:
    with pytest.raises(errors.InvalidRequestError, match='RefSeq protein HGVS'):
        hgvs.accepted_protein_hgvs('Mavedb', variant)


def test_both_forms_take_the_repeat_notation_clinvar_names_an_allele_under() -> None:
    """`Variant.Normalize` hands the caller such a rendering, so it has to take one back.

    The registry resolves it to the same canonical allele either way, and VariantValidator answers it
    with the tract expanded against the reference — which is the extent the string itself withholds,
    and why `coding_span` declines to place one.
    """
    title = 'NM_005249.5(FOXG1):c.219GCC[5] (p.Pro80del)'  # ClinVar's preferred name at a repeat locus
    assert hgvs.accepted_hgvs('Annotate', title) == 'NM_005249.5:c.219GCC[5]'
    assert hgvs.accepted_transcript_hgvs('Normalize', title) == 'NM_005249.5:c.219GCC[5]'
