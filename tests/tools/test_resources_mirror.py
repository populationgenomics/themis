"""Tests for the manifest side of tools.resources.mirror: what a manifest may declare."""

from __future__ import annotations

import pathlib

import pytest

from tools.resources import mirror

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
    # `load_manifest` raises on a malformed entry or a duplicate destination; non-empty rules out a
    # vacuous pass.
    _, entries = mirror.load_manifest()
    assert entries


def test_every_declared_role_lands_under_its_assembly(tmp_path: pathlib.Path) -> None:
    extra = '\n'.join(
        f'{role} = {{ url = "https://example.org/{role}", suffix = "{sorted(mirror._ROLE_SUFFIXES[role])[0]}" }}'
        for role in mirror._ASSEMBLY_ROLES
    )
    _, entries = mirror.load_manifest(_manifest(tmp_path, extra))
    assert {entry.role for entry in entries} == {'genome', *mirror._ASSEMBLY_ROLES}
    assert all(entry.object.startswith('s/1/') for entry in entries)


@pytest.mark.parametrize('role', ['transcripts', 'ncrna', 'proteins'])
def test_a_sequence_set_must_be_gzipped_fasta(tmp_path: pathlib.Path, role: str) -> None:
    extra = f'{role} = {{ url = "https://example.org/{role}", suffix = "fa" }}'
    with pytest.raises(mirror.MirrorError, match='allowed are'):
        mirror.load_manifest(_manifest(tmp_path, extra))


def test_an_unknown_role_is_refused_rather_than_ignored(tmp_path: pathlib.Path) -> None:
    extra = 'transcript = { url = "https://example.org/t", suffix = "fa.gz" }'
    with pytest.raises(mirror.MirrorError, match='unknown'):
        mirror.load_manifest(_manifest(tmp_path, extra))
