"""The buf/validate stubs Themis commits are the ones every installed wheel ships beside them."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]
_STUBS = ('buf/validate/validate_pb2.py', 'buf/validate/validate_pb2.pyi')


def _record_hash(path: pathlib.Path) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b'=').decode()


def test_every_installed_buf_validate_stub_is_the_one_themis_commits() -> None:
    # Wheels unpack into one site-packages/buf/validate, so the file on disk is the last one installed; each
    # distribution's RECORD hash says what it shipped.
    committed = {stub: _record_hash(_REPO / stub) for stub in _STUBS}
    shipped = []
    for distribution in importlib.metadata.distributions():
        files = distribution.files
        assert files is not None, f'{distribution.name} has no RECORD'
        for file in files:
            if file.as_posix() in committed:
                assert file.hash is not None, f'{distribution.name} records {file} without a hash'
                shipped.append((distribution.name, file.as_posix(), file.hash.value))
    assert {name for name, _, _ in shipped} >= {'grus'}
    assert [entry for entry in shipped if entry[2] != committed[entry[1]]] == []
