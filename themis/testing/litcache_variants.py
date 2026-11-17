"""Stand-ins and synthetic reference builders for the variant extraction and validation steps."""

from __future__ import annotations

import collections.abc
import dataclasses
import gzip
import pathlib

from weaver_data_provider import refget
from weaver_data_provider.build import genome as genome_build
from weaver_data_provider.build import store as store_build
from weaver_data_provider.v1 import bundle_pb2

from themis.litcache import produce, variant_extract, variant_reference
from themis.litcache.models import variants_pb2

VALIDATION = variants_pb2.VariantValidationConstruction(
    weaver_version='0.0.0',
    stores=[variants_pb2.StoreVersion(name='refseq-grch38', release='test')],
    validator_version='0',
)


async def _extract(_markdown: str) -> variant_extract.Extraction:
    raise AssertionError('the variant extraction must not be called')


def _validate(
    _mention_set: variants_pb2.MentionSet, _text: str
) -> tuple[variants_pb2.MentionSet, variants_pb2.Verdicts, variants_pb2.VariantValidationConstruction]:
    raise AssertionError('the variant validation must not be called')


# Collaborators that refuse to be called.
UNUSED = produce.Variants(extract=_extract, validate=_validate, validation=VALIDATION)

CHROM_38 = 'NC_000001.11'
CHROM_37 = 'NC_000001.10'
UNIQUE_FLANK = 'AAGTTTGGATCC'
OVERLAPPING_FLANK = 'ATATATATATAT'

_CODONS = {
    'ATG': 'M',
    'AAA': 'K',
    'AAG': 'K',
    'AGA': 'R',
    'GCT': 'A',
    'ACC': 'T',
    'GAA': 'E',
    'CAA': 'Q',
    'CTG': 'L',
    'TTC': 'F',
    'CCA': 'P',
    'GTG': 'V',
    'ATC': 'I',
    'TGC': 'C',
    'AGT': 'S',
    'TAA': '*',
}


def _translate(cds: str) -> bytes:
    residues = ''.join(_CODONS[cds[i : i + 3]] for i in range(0, len(cds), 3))
    if not residues.endswith('*') or '*' in residues[:-1]:
        raise ValueError(f'invalid synthetic CDS translation: {residues}')
    return residues[:-1].encode('ascii')


@dataclasses.dataclass(frozen=True)
class _TranscriptSpec:
    accession: str
    version: int
    protein: str
    protein_version: int
    exons: tuple[tuple[int, str], ...]
    utr5_len: int = 0
    mane: bool = False
    publisher: bundle_pb2.Publisher = bundle_pb2.PUBLISHER_REFSEQ


def _write_exons(exons_spec: tuple[tuple[int, str], ...], genome_buf: bytearray) -> tuple[list[bundle_pb2.Exon], str]:
    exons: list[bundle_pb2.Exon] = []
    parts: list[str] = []
    t_offset = 0
    for g_start, seq in exons_spec:
        n = len(seq)
        genome_buf[g_start - 1 : g_start - 1 + n] = seq.encode('ascii')
        exons.append(
            bundle_pb2.Exon(
                transcript_start=t_offset,
                transcript_end=t_offset + n,
                genome_start=g_start - 1,
                genome_end_inclusive=g_start + n - 2,
                cigar=f'{n}=',
            )
        )
        parts.append(seq)
        t_offset += n
    return exons, ''.join(parts)


def _place_transcript(
    spec: _TranscriptSpec, genome_buf: bytearray
) -> tuple[bundle_pb2.Transcript, bundle_pb2.Protein, list[bundle_pb2.Sequence]]:
    exons, mrna = _write_exons(spec.exons, genome_buf)
    prot_bytes = _translate(mrna[spec.utr5_len :])
    mrna_digest = refget.digest(mrna.encode('ascii'))
    prot_digest = refget.digest(prot_bytes)
    alignment = bundle_pb2.Alignment(
        assembly=bundle_pb2.ASSEMBLY_GRCH38,
        chromosome=CHROM_38,
        strand=bundle_pb2.STRAND_PLUS,
        source=bundle_pb2.ALIGNMENT_SOURCE_NCBI_BAM,
        exons=exons,
    )
    transcript = bundle_pb2.Transcript(
        accession=spec.accession,
        version=spec.version,
        publisher=spec.publisher,
        status=bundle_pb2.TRANSCRIPT_STATUS_CURRENT,
        biotype='mRNA',
        tags=[bundle_pb2.TAG_MANE_SELECT] if spec.mane else [],
        sequence_digest=mrna_digest,
        cds=bundle_pb2.Cds(start_index=spec.utr5_len, end_index_inclusive=len(mrna) - 1),
        protein_accession=spec.protein,
        protein_version=spec.protein_version,
        alignments=[alignment],
    )
    protein = bundle_pb2.Protein(
        accession=spec.protein,
        version=spec.protein_version,
        sequence_digest=prot_digest,
        transcript_accession=spec.accession,
        transcript_version=spec.version,
    )
    sequences = [
        bundle_pb2.Sequence(digest=mrna_digest, alphabet=bundle_pb2.ALPHABET_NUCLEOTIDE, residues=mrna.encode('ascii')),
        bundle_pb2.Sequence(digest=prot_digest, alphabet=bundle_pb2.ALPHABET_PROTEIN, residues=prot_bytes),
    ]
    return transcript, protein, sequences


def _gene_bundle(
    symbol: str,
    gene_id: str,
    specs: collections.abc.Sequence[_TranscriptSpec],
    genome_buf: bytearray,
    *,
    previous_symbols: collections.abc.Sequence[str] = (),
) -> bundle_pb2.GeneBundle:
    transcripts: list[bundle_pb2.Transcript] = []
    proteins: list[bundle_pb2.Protein] = []
    by_digest: dict[str, bundle_pb2.Sequence] = {}
    for spec in specs:
        tx, prot, seqs = _place_transcript(spec, genome_buf)
        transcripts.append(tx)
        proteins.append(prot)
        for seq in seqs:
            by_digest[seq.digest] = seq
    return bundle_pb2.GeneBundle(
        gene=bundle_pb2.Gene(symbol=symbol, previous_symbols=list(previous_symbols), ncbi_gene_id=gene_id),
        transcripts=transcripts,
        proteins=proteins,
        sequences=list(by_digest.values()),
    )


def _write_genome(path: pathlib.Path, chrom: str, buf: bytearray, assembly: bundle_pb2.Assembly) -> None:
    fasta = path.parent / f'{chrom}.fa.gz'
    seq = buf.decode('ascii')
    with gzip.open(fasta, 'wt', encoding='ascii') as fh:
        fh.write(f'>{chrom}\n')
        for i in range(0, len(seq), 70):
            fh.write(seq[i : i + 70] + '\n')
    genome_build.build_genome(fasta, path, assembly=assembly)


def _write_store(
    path: pathlib.Path,
    bundles: collections.abc.Sequence[bundle_pb2.GeneBundle],
    release: str,
    assembly: bundle_pb2.Assembly,
) -> None:
    inputs_file = path.parent / f'{release}.txt'
    inputs_file.write_text(f'{release}\n', 'utf-8')
    shard = store_build.write_shard(
        list(bundles), path.parent / f'{release}-shards', release=release, inputs=[('src', inputs_file)]
    )
    store_build.write_index(path, [shard], assembly=assembly)


def _gene1_bundles(g38: bytearray) -> tuple[bundle_pb2.GeneBundle, bundle_pb2.GeneBundle]:
    utr5 = UNIQUE_FLANK + OVERLAPPING_FLANK + 'AT'  # 26 bp: UNIQUE_FLANK once, OVERLAPPING_FLANK twice overlapping
    cds_ex1 = 'ATGAAACTGAGAGCTTTCCCAGTG'  # 24 bp (c.1..24): M K L R A F P V
    cds_ex2_mane = 'ATCACCGAACAATGCAGTTAA'  # 21 bp (c.25..45): c.30 is C, c.40 is A
    cds_ex2_fit = 'ATCAAGGAACAATGCAGTTAA'  # 21 bp (c.25..45): c.30 is G, c.40 is A
    g38[150:156] = b'GTAAGT'  # splice donor after exon 1 at g.101..150, so c.24+2T>C holds
    mane_exons = ((101, utr5 + cds_ex1), (201, cds_ex2_mane))
    refseq = _gene_bundle(
        'GENE1',
        '101',
        [
            _TranscriptSpec('NM_000101', 1, 'NP_000101', 1, mane_exons, utr5_len=26, mane=True),
            _TranscriptSpec('NM_000102', 1, 'NP_000102', 1, ((101, utr5 + cds_ex1), (301, cds_ex2_fit)), utr5_len=26),
            _TranscriptSpec('NM_000103', 1, 'NP_000103', 1, ((401, cds_ex1), (451, cds_ex2_mane))),
        ],
        g38,
        previous_symbols=('OLDGENE1',),
    )
    ensembl = _gene_bundle(
        'GENE1',
        '101',
        [
            _TranscriptSpec(
                'ENST00000000101',
                1,
                'ENSP00000000101',
                1,
                mane_exons,
                utr5_len=26,
                mane=True,
                publisher=bundle_pb2.PUBLISHER_ENSEMBL,
            ),
        ],
        g38,
        previous_symbols=('OLDGENE1',),
    )
    return refseq, ensembl


def _ver1_bundle(g38: bytearray) -> bundle_pb2.GeneBundle:
    ver_prefix = 'ATGTTC' + 'CTG' * 16
    ver_v2_prefix = 'ATGTTC' + 'CTG' * 5 + 'CCA' + 'CTG' * 10
    ver202_ex1 = 'ATGGCTCTGTTCCCAGTG'  # M A L F P V (c.4G>A -> p.Ala2Thr)
    return _gene_bundle(
        'VER1',
        '200',
        [
            _TranscriptSpec('NM_000200', 1, 'NP_000200', 1, ((601, ver_prefix + 'GCTGAATAA'),)),
            _TranscriptSpec('NM_000200', 2, 'NP_000200', 2, ((701, ver_v2_prefix + 'ATCGAATAA'),)),
            _TranscriptSpec('NM_000200', 3, 'NP_000200', 3, ((801, ver_prefix + 'GTGGAATAA'),)),
            _TranscriptSpec('NM_000201', 1, 'NP_000201', 1, ((901, ver_prefix + 'TGCGAATAA'),)),
            _TranscriptSpec('NM_000202', 1, 'NP_000202', 1, ((1001, ver202_ex1), (1051, 'ATCGAACAAAGTTAA'))),
            _TranscriptSpec('NM_000202', 2, 'NP_000202', 2, ((1001, ver202_ex1), (1101, 'AGAGAACAAAGTTAA'))),
        ],
        g38,
    )


def build_references(root: pathlib.Path) -> variant_reference.References:
    """Build synthetic GRCh38/GRCh37 genomes and RefSeq/Ensembl stores under `root` and open `References`."""
    root.mkdir(parents=True, exist_ok=True)
    g38 = bytearray(('ACGT' * 750).encode('ascii'))
    g37 = bytearray(('ACGT' * 750).encode('ascii'))
    gene1_refseq, gene1_ensembl = _gene1_bundles(g38)
    ver1 = _ver1_bundle(g38)

    _write_genome(root / 'genome38', CHROM_38, g38, bundle_pb2.ASSEMBLY_GRCH38)
    _write_genome(root / 'genome37', CHROM_37, g37, bundle_pb2.ASSEMBLY_GRCH37)
    _write_store(root / 'refseq38', [gene1_refseq, ver1], 'REFSEQ_38', bundle_pb2.ASSEMBLY_GRCH38)
    _write_store(root / 'ensembl38', [gene1_ensembl], 'ENSEMBL_38', bundle_pb2.ASSEMBLY_GRCH38)
    _write_store(
        root / 'refseq37',
        [bundle_pb2.GeneBundle(gene=bundle_pb2.Gene(symbol='REF37', ncbi_gene_id='37'))],
        'REFSEQ_37',
        bundle_pb2.ASSEMBLY_GRCH37,
    )

    builds = variant_reference.Builds(
        refseq38='refseq38',
        ensembl38='ensembl38',
        genome38='genome38',
        refseq37='refseq37',
        genome37='genome37',
    )
    return variant_reference.References(str(root), builds)
