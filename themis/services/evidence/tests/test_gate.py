"""Admission stays where rpc-authorization.md puts it, asserted over the evidence image's AST."""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import types

import pytest

from themis.services import evidence
from themis.testing import gate


def _servicer_module_names() -> list[str]:
    root = pathlib.Path(evidence.__path__[0])
    per_interface = sorted(
        f'{evidence.__name__}.{child.name}.servicer' for child in root.iterdir() if (child / 'servicer.py').is_file()
    )
    return [f'{evidence.__name__}.serving', *per_interface]


def _load_module(name: str, path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_evidence_module_constructs_a_server() -> None:
    gate.assert_no_module_constructs_a_server(evidence)


def test_every_evidence_servicer_is_discovered() -> None:
    root = pathlib.Path(evidence.__path__[0])
    interfaces = {child.name for child in root.iterdir() if (child / 'interface.py').is_file()}
    servicers = {child.name for child in root.iterdir() if (child / 'servicer.py').is_file()}
    assert interfaces
    assert servicers == interfaces


@pytest.mark.parametrize('module_name', _servicer_module_names())
def test_no_evidence_servicer_reads_call_metadata(module_name: str) -> None:
    gate.assert_servicer_reads_no_call_metadata(importlib.import_module(module_name))


@pytest.mark.parametrize(
    'snippet',
    [
        pytest.param('import grpc.aio\ngrpc.aio.server()\n', id='grpc-aio-server'),
        pytest.param('import grpc\ngrpc.server()\n', id='grpc-sync-server'),
        pytest.param('from grpc import aio\naio.server()\n', id='from-grpc-import-aio'),
        pytest.param('from grpc.aio import server\nserver()\n', id='from-grpc-aio-import-server'),
    ],
)
def test_bare_server_construction_in_any_package_module_fails_the_gate(
    tmp_path: pathlib.Path,
    snippet: str,
) -> None:
    # Placed inside a parent named `tests` to verify only package-internal `tests/` directories are exempt.
    pkg_dir = tmp_path / 'tests' / 'sample_pkg'
    pkg_dir.mkdir(parents=True)
    init_py = pkg_dir / '__init__.py'
    init_py.write_text('', encoding='utf-8')
    (pkg_dir / 'tests').mkdir()
    (pkg_dir / 'tests' / 'helper.py').write_text(snippet, encoding='utf-8')
    pkg = _load_module('sample_pkg', init_py)
    gate.assert_no_module_constructs_a_server(pkg)

    (pkg_dir / 'serving.py').write_text(snippet, encoding='utf-8')
    with pytest.raises(AssertionError, match=r'serving\.py'):
        gate.assert_no_module_constructs_a_server(pkg)


def test_reading_invocation_metadata_in_a_servicer_fails_the_gate(tmp_path: pathlib.Path) -> None:
    servicer_py = tmp_path / 'bad_servicer.py'
    servicer_py.write_text('def handle(context):\n    return context.invocation_metadata()\n', encoding='utf-8')
    mod = _load_module('bad_servicer', servicer_py)
    with pytest.raises(AssertionError, match='bad_servicer'):
        gate.assert_servicer_reads_no_call_metadata(mod)
