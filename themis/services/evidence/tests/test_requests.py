"""Tests for the request preconditions the evidence interfaces share.

Unit tests on the validators rather than wire tests per rpc: one function serves several interfaces,
so what is under test here is the rule each holds a caller to. That an rpc runs one at all is a
separate property, and a separate suite: `test_request_wiring` drives every rpc taking a shared
check, and the two that serve a single rpc (`require_cds_range`, `require_mondo_id`) are driven by
that interface's own suite.
"""

from __future__ import annotations

import pytest

from themis.services.evidence import errors, requests


@pytest.mark.parametrize(
    'gnomad_id',
    [
        '17-31232881-G-C',  # what the variant interface emits: no prefix, uppercase
        '1-100-A-T',
        '22-100-A-T',  # the last autosome, so the two-digit range is not open-ended
        'X-100-A-T',
        'Y-100-A-T',
        'MT-100-A-T',
        'chr17-31232881-G-C',  # the prefix nothing here emits but callers do carry
    ],
)
def test_the_positional_forms_the_variant_interface_hands_out_are_accepted(gnomad_id: str) -> None:
    requests.require_positional_id('DescribeVariant', 'gnomad_id', gnomad_id)


@pytest.mark.parametrize(
    'variant',
    [
        '',
        'NM_001042492.3:c.3496G>C',  # an HGVS, not a positional id
        'rs80357906',
        'CA123456',
        '17:31232881:G:C',  # colon-separated, which neither upstream parses
        '17-31232881-g-c',  # lowercase alleles: gnomAD tolerates them, the Broad services do not
        '0-100-A-T',
        '23-100-A-T',  # not a contig either upstream holds, though both answer it as unscorable
        '99-100-A-T',
    ],
)
def test_an_id_the_upstream_would_score_as_absent_is_refused(variant: str) -> None:
    """Both upstreams answer an unparsable id with a 200 carrying an absence, not an error.

    For the two positional rpcs that absence is the finding — rarity, and an unscorable position — so
    without the check a caller's typo comes back as evidence about the variant, and is never retried.
    """
    with pytest.raises(errors.InvalidRequestError, match='chrom-pos-ref-alt'):
        requests.require_positional_id('DescribeVariant', 'gnomad_id', variant)


def test_an_over_long_id_is_refused_for_its_length_and_not_for_its_shape() -> None:
    """A well-formed id reported as the wrong KIND of identifier sends the caller after a typo.

    The two are separate faults with separate remedies, so they get separate messages: this one is a
    real allele the transport cannot carry, not a malformed field.
    """
    over_long = f'X-18657373-{"A" * 4100}-A'
    with pytest.raises(errors.InvalidRequestError) as caught:
        requests.require_positional_id('PredictDeltas', 'variant', over_long)
    detail = str(caught.value)
    assert 'chrom-pos-ref-alt' not in detail
    assert str(len(over_long)) in detail
    # The whole id would crowd the diagnosis out of the trailer bound, taking the message with it.
    assert len(detail) < len(over_long)


def test_a_kilobase_deletion_id_is_a_well_formed_allele() -> None:
    """`Variant.Normalize` hands out this id; both upstreams answer it, so neither rpc may refuse it."""
    deletion = f'X-18657373-{"ACGT" * 251}-A'  # 1005 deleted bases: the RS1 case's own id length
    assert len(deletion) > 1000
    requests.require_positional_id('DescribeVariant', 'gnomad_id', deletion)


@pytest.mark.parametrize(
    'transcript',
    [
        '',
        'NM_001042492',  # unversioned: names an exon structure the caller cannot reproduce
        'ENST00000380152.8',  # Ensembl: VariantValidator holds no record of it
        'NR_003051.3',  # non-coding: no CDS to report a c. span or translate a skip from
        'NM_001042492.3:c.3496G>C',  # an HGVS, not the bare accession
        'NP_001035957.1',
    ],
)
def test_a_transcript_the_exon_table_cannot_be_looked_up_by_is_refused(transcript: str) -> None:
    with pytest.raises(errors.InvalidRequestError, match='RefSeq coding transcript accession'):
        requests.require_transcript('GetStructure', transcript)


@pytest.mark.parametrize('genome_build', ['', 'GRCh39', 'hg38'])
def test_a_genome_build_the_upstreams_do_not_align_to_is_refused(genome_build: str) -> None:
    with pytest.raises(errors.InvalidRequestError, match='genome_build'):
        requests.require_genome_build('GetStructure', genome_build)


@pytest.mark.parametrize('genome_build', ['GRCh38', 'GRCh37'])
def test_the_builds_the_upstreams_align_to_are_accepted(genome_build: str) -> None:
    requests.require_genome_build('GetStructure', genome_build)


@pytest.mark.parametrize(
    ('start', 'end'),
    [
        (0, 10),  # c. numbering has no 0, so this is where an unset field is caught
        (10, 0),
        (20, 10),  # descending
        (-1, -20),  # descending across the 5'UTR, where integer order is still transcript order
    ],
)
def test_a_c_range_that_is_not_in_transcript_order_is_refused(start: int, end: int) -> None:
    with pytest.raises(errors.InvalidRequestError):
        requests.require_cds_range('SearchCodingSpan', start, end)


@pytest.mark.parametrize(('start', 'end'), [(1, 1), (1, 100), (-20, -1), (-20, 100)])
def test_a_c_range_in_transcript_order_is_accepted(start: int, end: int) -> None:
    requests.require_cds_range('SearchCodingSpan', start, end)


def test_a_gene_is_stripped_so_no_upstream_is_sent_the_padding() -> None:
    assert requests.require_gene('DescribeVariant', '  BRCA1 ', purpose='for the gene pool') == 'BRCA1'


@pytest.mark.parametrize('gene', ['', '   '])
def test_an_empty_gene_is_refused(gene: str) -> None:
    with pytest.raises(errors.InvalidRequestError, match='HGNC symbol'):
        requests.require_gene('DescribeVariant', gene, purpose='for the gene pool')


def test_an_unset_hgnc_id_is_refused_as_unset_and_not_as_the_wrong_shape() -> None:
    """The remedies differ: one caller has an id to supply, the other put something else in the field."""
    with pytest.raises(errors.InvalidRequestError, match='no hgnc_id') as caught:
        requests.require_hgnc_id('DescribeGene', '')
    assert 'and digits' not in str(caught.value)


@pytest.mark.parametrize('hgnc_id', ['  ', 'BRCA1', 'HGNC:', 'HGNC:1100.1', '1100', ' HGNC:1100 '])
def test_a_gene_that_is_not_an_hgnc_id_is_refused(hgnc_id: str) -> None:
    """None of these keys the reference tables, so unchecked each answers "no source carries this gene"."""
    with pytest.raises(errors.InvalidRequestError, match='"HGNC:" and digits'):
        requests.require_hgnc_id('DescribeGene', hgnc_id)


@pytest.mark.parametrize('hgnc_id', ['HGNC:1100', 'hgnc:1100'])
def test_an_hgnc_id_is_accepted_in_either_casing_the_tables_resolve(hgnc_id: str) -> None:
    """The tables key on the id upper-cased, so refusing the lowercase form would refuse a live answer."""
    requests.require_hgnc_id('DescribeGene', hgnc_id)


@pytest.mark.parametrize('mondo_id', ['', 'MONDO:123', 'MONDO:12345678', '0007254', 'Orphanet:145'])
def test_a_disease_entity_that_is_not_a_mondo_curie_is_refused(mondo_id: str) -> None:
    with pytest.raises(errors.InvalidRequestError, match='MONDO curie'):
        requests.require_mondo_id('DescribeGene', mondo_id)


def test_a_mondo_curie_is_accepted() -> None:
    requests.require_mondo_id('DescribeGene', 'MONDO:0007254')
