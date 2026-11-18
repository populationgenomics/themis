"""Moving a change from an older version of its accession to the newest, where the two are the same around it."""

from __future__ import annotations

import pathlib

import pytest
from weaver_data_provider import refget
from weaver_data_provider.build import store as store_build
from weaver_data_provider.v1 import bundle_pb2

from themis.litcache import variant_fit, variant_reference, variant_versions
from themis.litcache.models import variants_pb2
from themis.testing import litcache_variants

# Unplaced coding transcripts whose 5' UTR grew or shrank by five bases, and a 61-residue protein series
# differing only at residue 31 (`Y` in .1 and .2, `A` in .3).
_CODING = 'ATG' + 'GCTAGCATCG' * 12 + 'TAA'
_PROT_OLD = ('M' + 'ACDEFGHIKL' * 2 + 'MNPQRSTVW' + 'Y' + 'ACDEFGHIKL' * 3).encode('ascii')
_PROT_NEW = ('M' + 'ACDEFGHIKL' * 2 + 'MNPQRSTVW' + 'A' + 'ACDEFGHIKL' * 3).encode('ascii')


def _unplaced_transcript(
    accession: str,
    version: int,
    mrna: str,
    *,
    utr5_len: int,
    protein: str,
    protein_version: int,
) -> tuple[bundle_pb2.Transcript, bundle_pb2.Sequence]:
    raw = mrna.encode('ascii')
    digest = refget.digest(raw)
    n = len(mrna)
    # Aligned to an unplaced contig absent from genome38: get_transcript reads cds_start, while genomic projection
    # cannot resolve NT_999999.1 and falls back to the transcript sequence window.
    alignment = bundle_pb2.Alignment(
        assembly=bundle_pb2.ASSEMBLY_GRCH38,
        chromosome='NT_999999.1',
        strand=bundle_pb2.STRAND_PLUS,
        source=bundle_pb2.ALIGNMENT_SOURCE_NCBI_BAM,
        exons=[
            bundle_pb2.Exon(
                transcript_start=0,
                transcript_end=n,
                genome_start=0,
                genome_end_inclusive=n - 1,
                cigar=f'{n}=',
            )
        ],
    )
    transcript = bundle_pb2.Transcript(
        accession=accession,
        version=version,
        publisher=bundle_pb2.PUBLISHER_REFSEQ,
        status=bundle_pb2.TRANSCRIPT_STATUS_CURRENT,
        biotype='mRNA',
        sequence_digest=digest,
        cds=bundle_pb2.Cds(start_index=utr5_len, end_index_inclusive=n - 1),
        protein_accession=protein,
        protein_version=protein_version,
        alignments=[alignment],
    )
    return transcript, bundle_pb2.Sequence(digest=digest, alphabet=bundle_pb2.ALPHABET_NUCLEOTIDE, residues=raw)


def _protein_record(
    accession: str,
    version: int,
    residues: bytes,
    *,
    transcript: str,
    transcript_version: int,
) -> tuple[bundle_pb2.Protein, bundle_pb2.Sequence]:
    digest = refget.digest(residues)
    record = bundle_pb2.Protein(
        accession=accession,
        version=version,
        sequence_digest=digest,
        transcript_accession=transcript,
        transcript_version=transcript_version,
    )
    return record, bundle_pb2.Sequence(digest=digest, alphabet=bundle_pb2.ALPHABET_PROTEIN, residues=residues)


def _unplaced_bundle() -> bundle_pb2.GeneBundle:
    transcripts: list[bundle_pb2.Transcript] = []
    proteins: list[bundle_pb2.Protein] = []
    by_digest: dict[str, bundle_pb2.Sequence] = {}
    tx_specs = (
        ('NM_000301', 1, 'GGGGGGGGGG' + _CODING, 10, 'NP_000301', 1, _PROT_OLD),
        ('NM_000301', 2, 'TTTTTGGGGGGGGGG' + _CODING, 15, 'NP_000301', 2, _PROT_NEW),
        ('NM_000302', 1, 'TTTTTGGGGGGGGGG' + _CODING, 15, 'NP_000302', 1, _PROT_OLD),
        ('NM_000302', 2, 'GGGGGGGGGG' + _CODING, 10, 'NP_000302', 2, _PROT_OLD),
        ('NM_000303', 1, _CODING, 0, 'NP_000303', 1, _PROT_OLD),
        ('NM_000303', 2, _CODING, 0, 'NP_000303', 2, _PROT_OLD),
        ('NM_000303', 3, _CODING, 0, 'NP_000303', 3, _PROT_NEW),
    )
    for tx_acc, tx_ver, mrna, utr5_len, prot_acc, prot_ver, prot_seq in tx_specs:
        tx, tx_seq = _unplaced_transcript(
            tx_acc, tx_ver, mrna, utr5_len=utr5_len, protein=prot_acc, protein_version=prot_ver
        )
        prot, p_seq = _protein_record(prot_acc, prot_ver, prot_seq, transcript=tx_acc, transcript_version=tx_ver)
        transcripts.append(tx)
        proteins.append(prot)
        by_digest[tx_seq.digest] = tx_seq
        by_digest[p_seq.digest] = p_seq
    return bundle_pb2.GeneBundle(
        gene=bundle_pb2.Gene(symbol='VER2', ncbi_gene_id='301'),
        transcripts=transcripts,
        proteins=proteins,
        sequences=list(by_digest.values()),
    )


@pytest.fixture(scope='module')
def references(tmp_path_factory: pytest.TempPathFactory) -> variant_reference.References:
    root: pathlib.Path = tmp_path_factory.mktemp('variant-versions-refs')
    litcache_variants.build_references(root)
    existing_shards = [
        p for p in sorted((root / 'REFSEQ_38-shards').glob('*.bagz')) if not p.name.endswith('.shard.bagz')
    ]
    inputs_file = root / 'VER2.txt'
    inputs_file.write_text('VER2\n', 'utf-8')
    extra_shard = store_build.write_shard(
        [_unplaced_bundle()],
        root / 'VER2-shards',
        release='REFSEQ_38',
        inputs=[('src', inputs_file)],
    )
    store_build.write_index(
        root / 'refseq38',
        [*existing_shards, extra_shard],
        assembly=bundle_pb2.ASSEMBLY_GRCH38,
    )
    builds = variant_reference.Builds(
        refseq38='refseq38',
        ensembl38='ensembl38',
        genome38='genome38',
        refseq37='refseq37',
        genome37='genome37',
    )
    return variant_reference.References(str(root), builds)


def _updated(references: variant_reference.References, *hgvs: str) -> list[str]:
    mention = variants_pb2.Mention(verbatim='x', candidates=[variants_pb2.Candidate(hgvs=h) for h in hgvs])
    out = variant_versions.update(variants_pb2.MentionSet(mentions=[mention]), references)
    return [c.hgvs for c in out.mentions[0].candidates]


def test_a_protein_change_far_from_the_versions_difference_moves_to_the_newest(
    references: variant_reference.References,
) -> None:
    assert _updated(references, 'NP_000301.1:p.Glu4Lys') == ['NP_000301.1:p.Glu4Lys', 'NP_000301.2:p.Glu4Lys']


def test_a_protein_change_near_the_versions_difference_stays(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.1:p.Glu24Lys') == ['NP_000301.1:p.Glu24Lys']


def test_a_coding_change_is_compared_in_coding_coordinates(references: variant_reference.References) -> None:
    assert _updated(references, 'NM_000301.1:c.4G>A') == ['NM_000301.1:c.4G>A', 'NM_000301.2:c.4G>A']


def test_placed_versions_are_compared_by_their_projection(references: variant_reference.References) -> None:
    # NM_000202.1 and .2 share exon 1 on GRCh38 (same genomic projection) while differing inside the 30-bp window;
    # NM_000200.1 and .3 share the first 54 coding bases while placed at different loci on GRCh38.
    assert _updated(references, 'NM_000202.1:c.4G>A') == ['NM_000202.1:c.4G>A', 'NM_000202.2:c.4G>A']
    assert _updated(references, 'NM_000200.1:c.4T>C') == ['NM_000200.1:c.4T>C']


def test_an_intronic_change_needs_a_projection(references: variant_reference.References) -> None:
    assert _updated(references, 'NM_000301.1:c.4+1G>A') == ['NM_000301.1:c.4+1G>A']


def test_nothing_is_added_on_the_newest_or_beside_it(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.2:p.Glu4Lys') == ['NP_000301.2:p.Glu4Lys']
    assert _updated(references, 'NP_000301.1:p.Glu4Lys', 'NP_000301.2:p.Glu4Lys') == [
        'NP_000301.1:p.Glu4Lys',
        'NP_000301.2:p.Glu4Lys',
    ]


def test_the_moved_change_keeps_its_rewrites_and_is_dropped_as_derived(
    references: variant_reference.References,
) -> None:
    candidate = variants_pb2.Candidate(hgvs='NP_000301.1:p.Glu4Lys')
    candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_NOTATION)
    mention_set = variants_pb2.MentionSet(mentions=[variants_pb2.Mention(verbatim='E4K', candidates=[candidate])])
    out = variant_versions.update(mention_set, references)
    moved = out.mentions[0].candidates[1]
    assert moved.hgvs == 'NP_000301.2:p.Glu4Lys'
    assert [r.kind for r in moved.rewrites] == [
        variants_pb2.REWRITE_KIND_NOTATION,
        variants_pb2.REWRITE_KIND_VERSION_UPDATE,
    ]
    assert moved.rewrites[1].source == 'NP_000301.1:p.Glu4Lys'
    stripped = variant_fit.without_derived(out)
    assert [c.hgvs for c in stripped.mentions[0].candidates] == ['NP_000301.1:p.Glu4Lys']
    assert [r.kind for r in stripped.mentions[0].candidates[0].rewrites] == [variants_pb2.REWRITE_KIND_NOTATION]


def test_a_change_without_a_position_stays(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.1:p.(?)') == ['NP_000301.1:p.(?)']


def test_a_change_past_the_end_of_both_versions_stays(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.1:p.Glu900Lys') == ['NP_000301.1:p.Glu900Lys']


def test_a_change_near_the_end_of_versions_that_end_alike_moves(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.1:p.Lys60Pro') == ['NP_000301.1:p.Lys60Pro', 'NP_000301.2:p.Lys60Pro']


def test_two_older_versions_of_one_change_gain_one_update(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000303.1:p.Glu4Lys', 'NP_000303.2:p.Glu4Lys') == [
        'NP_000303.1:p.Glu4Lys',
        'NP_000303.2:p.Glu4Lys',
        'NP_000303.3:p.Glu4Lys',
    ]


def test_an_allele_of_several_changes_stays(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.1:p.[Glu4Lys;Lys9Glu]') == ['NP_000301.1:p.[Glu4Lys;Lys9Glu]']


def test_a_coding_change_on_a_protein_stays(references: variant_reference.References) -> None:
    assert _updated(references, 'NP_000301.1:c.4G>A') == ['NP_000301.1:c.4G>A']


@pytest.mark.parametrize(
    ('hgvs', 'expected'),
    [
        ('NM_000301.1:c.-5G>A', ['NM_000301.1:c.-5G>A', 'NM_000301.2:c.-5G>A']),
        ('NM_000301.1:c.-14T>C', ['NM_000301.1:c.-14T>C']),
        ('NM_000302.1:c.-14T>C', ['NM_000302.1:c.-14T>C']),
    ],
)
def test_a_utr_position_moves_only_where_both_versions_hold_it(
    references: variant_reference.References, hgvs: str, expected: list[str]
) -> None:
    assert _updated(references, hgvs) == expected


def test_a_derived_candidate_is_refused(references: variant_reference.References) -> None:
    candidate = variants_pb2.Candidate(hgvs='NP_000301.1:p.Glu4Lys')
    candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT)
    mention_set = variants_pb2.MentionSet(mentions=[variants_pb2.Mention(verbatim='E4K', candidates=[candidate])])
    with pytest.raises(ValueError, match='derived'):
        variant_versions.update(mention_set, references)
