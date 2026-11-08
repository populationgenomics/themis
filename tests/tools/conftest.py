"""Fixtures for the reference tools' tests: a small manifest, and objects mirrored into an emulated bucket."""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
from collections.abc import Callable

import pytest
from google.cloud import storage

from tools.resources import manifest, mirror

_BAM = 'https://example.org/h.bam'
REFERENCE_MANIFEST = f"""
dataset = "reference"

[[assembly]]
id = "ncbi-grch38"
source = "ncbi"
version = "GCF_1.40_GRCh38"
release = "GCF_1.40-RS_2025_08"
seqid = "refseq-accession"
licence = "public-domain"
consumer = "tests"
genome = {{ url = "https://example.org/genome.fa.gz", suffix = "fa.gz" }}
annotation = {{ url = "https://example.org/genomic.gff.gz", suffix = "gff3.gz" }}
transcripts = {{ url = "https://example.org/rna.fa.gz", suffix = "fa.gz" }}
proteins = {{ url = "https://example.org/protein.fa.gz", suffix = "fa.gz" }}
records = {{ url = "https://example.org/rna.gbff.gz", suffix = "gbff.gz" }}
alignments = {{ url = "{_BAM}.a", suffix = "bam", index = {{ url = "{_BAM}.a.bai", extension = "bai" }} }}

[assembly.historical]
version = "GCF_1.40-RS_2024_08_historical"
annotation = {{ url = "https://example.org/h.gff.gz", suffix = "gff3.gz" }}
records = {{ url = "https://example.org/h.gbff.gz", suffix = "gbff.gz" }}
alignments = {{ url = "{_BAM}", suffix = "bam", index = {{ url = "{_BAM}.bai", extension = "bai" }} }}

[[assembly]]
id = "ncbi-grch37"
source = "ncbi"
version = "GCF_1.25_GRCh37"
release = "GCF_1.25-RS_2024_09"
seqid = "refseq-accession"
licence = "public-domain"
consumer = "tests"
genome = {{ url = "https://example.org/37/genome.fa.gz", suffix = "fa.gz" }}
annotation = {{ url = "https://example.org/37/genomic.gff.gz", suffix = "gff3.gz" }}
transcripts = {{ url = "https://example.org/37/rna.fa.gz", suffix = "fa.gz" }}
proteins = {{ url = "https://example.org/37/protein.fa.gz", suffix = "fa.gz" }}
records = {{ url = "https://example.org/37/rna.gbff.gz", suffix = "gbff.gz" }}
alignments = {{ url = "{_BAM}.37", suffix = "bam", index = {{ url = "{_BAM}.37.bai", extension = "bai" }} }}

[[assembly]]
id = "ensembl-grch38"
source = "ensembl"
version = "116_GRCh38"
release = "116"
seqid = "ensembl-name"
licence = "apache-2.0"
consumer = "tests"
genome = {{ url = "https://example.org/e.fa.gz", suffix = "fa.gz" }}
whole-annotation = {{ url = "https://example.org/e.gff3.gz", suffix = "gff3.gz" }}
whole-annotation-gtf = {{ url = "https://example.org/e.gtf.gz", suffix = "gtf.gz" }}
transcripts = {{ url = "https://example.org/e.cdna.fa.gz", suffix = "fa.gz" }}
ncrna = {{ url = "https://example.org/e.ncrna.fa.gz", suffix = "fa.gz" }}
proteins = {{ url = "https://example.org/e.pep.fa.gz", suffix = "fa.gz" }}

[[table]]
id = "mane-summary"
source = "mane"
version = "1.5_GRCh38"
role = "summary"
suffix = "tsv.gz"
url = "https://example.org/mane.tsv.gz"
licence = "public-domain"
consumer = "tests"

[[table]]
id = "hgnc-complete-set"
source = "hgnc"
version = "2026-07-07"
role = "complete-set"
suffix = "tsv"
url = "https://example.org/hgnc.tsv"
licence = "cc0-1.0"
consumer = "tests"
"""

# Mirrors one entry's bytes into the bucket, with the sidecar the mirror would write.
PutMirrored = Callable[[manifest.Entry, bytes], None]


def gbff(*versions: str) -> bytes:
    """A gzipped GenBank file holding one empty record per version, as far as a VERSION scan reads it."""
    return gzip.compress(''.join(f'LOCUS       X\nVERSION     {v}\n//\n' for v in versions).encode())


@pytest.fixture
def pinned(tmp_path: pathlib.Path) -> manifest.Manifest:
    path = tmp_path / 'reference.toml'
    path.write_text(REFERENCE_MANIFEST, 'utf-8')
    return manifest.load(path)


@pytest.fixture
def put_mirrored(gcs_bucket: storage.Bucket, pinned: manifest.Manifest) -> PutMirrored:
    def put(entry: manifest.Entry, data: bytes) -> None:
        name = f'{pinned.dataset}/{entry.object}'
        gcs_bucket.blob(name).upload_from_string(data)
        sidecar = {'url': entry.url, 'digest': hashlib.md5(data).hexdigest(), 'digest_algorithm': 'md5'}  # noqa: S324
        gcs_bucket.blob(name + mirror.PROVENANCE_SUFFIX).upload_from_string(json.dumps(sidecar))

    return put
