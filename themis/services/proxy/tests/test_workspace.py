"""Tests for workspace pack/unpack and extraction hardening (crafted-archive attacks)."""

from __future__ import annotations

import io
import pathlib
import tarfile

import pytest

from themis.services.proxy import workspace


def _tar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _tar_gz(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _tar_link(name: str, target: str, *, link_type: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        info = tarfile.TarInfo(name)
        info.type = link_type
        info.linkname = target
        tar.addfile(info)
    return buffer.getvalue()


def _tar_symlink(name: str, target: str) -> bytes:
    return _tar_link(name, target, link_type=tarfile.SYMTYPE)


def _tar_hardlink(name: str, target: str) -> bytes:
    return _tar_link(name, target, link_type=tarfile.LNKTYPE)


def test_round_trips_files(tmp_path: pathlib.Path) -> None:
    source = tmp_path / 'src'
    (source / 'a').mkdir(parents=True)
    (source / 'a' / 'f.txt').write_bytes(b'hello')
    (source / 'g.txt').write_bytes(b'world')

    dest = tmp_path / 'dst'
    dest.mkdir()
    workspace.unpack(workspace.pack(source, exclude=set()), dest)
    assert (dest / 'a' / 'f.txt').read_bytes() == b'hello'
    assert (dest / 'g.txt').read_bytes() == b'world'


def test_pack_excludes_the_durable_paths(tmp_path: pathlib.Path) -> None:
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'keep').write_bytes(b'k')
    (source / 'doc.md').write_bytes(b'd')

    dest = tmp_path / 'dst'
    dest.mkdir()
    workspace.unpack(workspace.pack(source, exclude={source / 'doc.md'}), dest)
    assert (dest / 'keep').exists()
    assert not (dest / 'doc.md').exists()


@pytest.mark.parametrize('name', ['../escape', 'a/../../escape'])
def test_rejects_a_traversing_path(tmp_path: pathlib.Path, name: str) -> None:
    with pytest.raises(workspace.UnsafeArchiveError):
        workspace.unpack(_tar({name: b'x'}), tmp_path)


def test_neutralizes_an_absolute_path(tmp_path: pathlib.Path) -> None:
    # the data filter strips the leading slash, so an absolute member lands inside dest, not at /.
    workspace.unpack(_tar({'/etc/passwd': b'x'}), tmp_path)
    assert (tmp_path / 'etc' / 'passwd').read_bytes() == b'x'


def test_rejects_an_escaping_symlink(tmp_path: pathlib.Path) -> None:
    with pytest.raises(workspace.UnsafeArchiveError):
        workspace.unpack(_tar_symlink('link', '../../etc/passwd'), tmp_path)


def test_rejects_an_escaping_hardlink(tmp_path: pathlib.Path) -> None:
    with pytest.raises(workspace.UnsafeArchiveError):
        workspace.unpack(_tar_hardlink('a/b/link', '../../../etc/passwd'), tmp_path)


def test_rejects_too_many_entries(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, '_MAX_ENTRIES', 2)
    with pytest.raises(workspace.UnsafeArchiveError):
        workspace.unpack(_tar({'a': b'x', 'b': b'y', 'c': b'z'}), tmp_path)


def test_rejects_oversized_archive(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace, '_MAX_TOTAL_BYTES', 10)
    with pytest.raises(workspace.UnsafeArchiveError):
        workspace.unpack(_tar({'big': b'x' * 100}), tmp_path)


def test_rejects_a_compressed_archive(tmp_path: pathlib.Path) -> None:
    # pack emits uncompressed, so the reader is uncompressed-only: a compressed archive (a compromised
    # store's decompression bomb) is rejected at open, before getmembers could expand it.
    with pytest.raises(workspace.UnsafeArchiveError):
        workspace.unpack(_tar_gz({'a': b'x'}), tmp_path)
