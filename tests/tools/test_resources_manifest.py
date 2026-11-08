"""Tests for tools.resources.manifest: what a manifest may declare."""

from __future__ import annotations

import pathlib

import pytest

from tools.resources import manifest

_ASSEMBLY = """
dataset = "reference"

[[assembly]]
id = "a"
source = "s"
version = "1"
seqid = "ucsc-name"
licence = "public-domain"
consumer = "tests"
genome = {{ url = "https://example.org/g.fa.gz", suffix = "fa.gz" }}
{extra}
"""


def _manifest(tmp_path: pathlib.Path, extra: str = '') -> pathlib.Path:
    path = tmp_path / 'reference.toml'
    path.write_text(_ASSEMBLY.format(extra=extra), 'utf-8')
    return path


def test_committed_manifest_loads() -> None:
    # `load` raises on a malformed entry or a duplicate destination; non-empty rules out a
    # vacuous pass.
    entries = manifest.load().entries
    assert entries


def test_every_declared_role_lands_under_its_assembly(tmp_path: pathlib.Path) -> None:
    extra = '\n'.join(
        f'{role} = {{ url = "https://example.org/{role}", suffix = "{sorted(manifest.ROLE_SUFFIXES[role])[0]}" }}'
        for role in manifest.ASSEMBLY_ROLES
    )
    entries = manifest.load(_manifest(tmp_path, extra)).entries
    assert {entry.role for entry in entries} == {'genome', *manifest.ASSEMBLY_ROLES}
    assert all(entry.object.startswith('s/1/') for entry in entries)


@pytest.mark.parametrize('role', ['transcripts', 'ncrna', 'proteins'])
def test_a_sequence_set_must_be_gzipped_fasta(tmp_path: pathlib.Path, role: str) -> None:
    extra = f'{role} = {{ url = "https://example.org/{role}", suffix = "fa" }}'
    with pytest.raises(manifest.ManifestError, match='allowed are'):
        manifest.load(_manifest(tmp_path, extra))


def test_an_unknown_role_is_refused_rather_than_ignored(tmp_path: pathlib.Path) -> None:
    extra = 'transcript = { url = "https://example.org/t", suffix = "fa.gz" }'
    with pytest.raises(manifest.ManifestError, match='unknown'):
        manifest.load(_manifest(tmp_path, extra))


def test_a_nested_index_lands_beside_its_object_under_the_composed_name(tmp_path: pathlib.Path) -> None:
    extra = (
        'alignments = { url = "https://example.org/a.bam", suffix = "bam", '
        'index = { url = "https://example.org/a.bam.bai", extension = "bai" } }'
    )
    entries = manifest.load(_manifest(tmp_path, extra)).entries
    alignments = [entry for entry in entries if entry.role == 'alignments']
    assert {entry.suffix for entry in alignments} == {'bam', 'bam.bai'}
    assert len({entry.object.rsplit('.', 2)[0] for entry in alignments}) == 1


def test_an_index_declares_only_the_extension_it_adds(tmp_path: pathlib.Path) -> None:
    extra = (
        'alignments = { url = "https://example.org/a.bam", suffix = "bam", '
        'index = { url = "https://example.org/a.bai", suffix = "bai", extension = "bai" } }'
    )
    with pytest.raises(manifest.ManifestError, match='both a suffix and an extension'):
        manifest.load(_manifest(tmp_path, extra))


def test_an_index_without_an_extension_is_refused(tmp_path: pathlib.Path) -> None:
    extra = (
        'alignments = { url = "https://example.org/a.bam", suffix = "bam", '
        'index = { url = "https://example.org/a.bai" } }'
    )
    with pytest.raises(manifest.ManifestError, match='must declare the extension'):
        manifest.load(_manifest(tmp_path, extra))


def test_an_index_under_an_index_is_refused(tmp_path: pathlib.Path) -> None:
    extra = (
        'alignments = { url = "https://example.org/a.bam", suffix = "bam", '
        'index = { url = "https://example.org/a.bai", extension = "bai", '
        'index = { url = "https://example.org/a.bai.x", extension = "x" } } }'
    )
    with pytest.raises(manifest.ManifestError, match='index under an index'):
        manifest.load(_manifest(tmp_path, extra))


_BAM = 'https://example.org/h.bam'
# The fixture's one assembly is the last of the `[[assembly]]` array, so its sub-table is `[assembly.historical]`.
_HISTORICAL = f"""
[assembly.historical]
version = "h1"
annotation = {{{{ url = "https://example.org/h.gff.gz", suffix = "gff3.gz" }}}}
records = {{{{ url = "https://example.org/h.gbff.gz", suffix = "gbff.gz" }}}}
alignments = {{{{ url = "{_BAM}", suffix = "bam", index = {{{{ url = "{_BAM}.bai", extension = "bai" }}}} }}}}
{{extra}}
"""


def _historical(extra: str = '') -> str:
    return _HISTORICAL.format(extra=extra)


def test_a_historical_set_lands_under_its_own_version_with_its_assemblys_source(tmp_path: pathlib.Path) -> None:
    assembly = manifest.load(_manifest(tmp_path, _historical())).assembly('a')
    historical = {path: entry for path, entry in assembly.objects.items() if path.startswith('historical/')}
    assert set(historical) == {
        'historical/annotation',
        'historical/records',
        'historical/alignments',
        'historical/alignments/index',
    }
    assert all(entry.object.startswith('s/h1/') for entry in historical.values())
    assert assembly.historical == 'h1'


def test_a_historical_set_missing_a_role_is_refused(tmp_path: pathlib.Path) -> None:
    extra = _historical().replace('records = { url = "https://example.org/h.gbff.gz", suffix = "gbff.gz" }\n', '')
    with pytest.raises(manifest.ManifestError, match='declares no records'):
        manifest.load(_manifest(tmp_path, extra))


def test_a_historical_set_refuses_what_it_inherits(tmp_path: pathlib.Path) -> None:
    with pytest.raises(manifest.ManifestError, match='unknown'):
        manifest.load(_manifest(tmp_path, _historical('seqid = "refseq-accession"')))


def test_a_release_must_be_a_name(tmp_path: pathlib.Path) -> None:
    with pytest.raises(manifest.ManifestError, match='not a name'):
        manifest.load(_manifest(tmp_path, 'release = 116'))


def test_an_absent_object_is_named_rather_than_a_key_error(tmp_path: pathlib.Path) -> None:
    assembly = manifest.load(_manifest(tmp_path)).assembly('a')
    with pytest.raises(manifest.ManifestError, match='a declares no records'):
        assembly.object('records')
