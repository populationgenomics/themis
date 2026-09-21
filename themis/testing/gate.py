"""The properties that keep admission where rpc-authorization.md puts it, asserted over an image's source.

Three checks, read off the source the way the grants tests read the program: the entrypoint builds its
server through ``interceptor.gated_server`` and no other way; no module of the image constructs a bare
server; and a servicer never reads the call's metadata itself, so an identity header has exactly one
reader, the interceptor. Each image's test module calls them against its own modules, so the next image
is covered by three lines rather than by copying the tests.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import types

from themis.clients.auth import interceptor as interceptor_mod

_BARE_SERVER = 'grpc.aio.server('


def _source(module: types.ModuleType) -> str:
    return pathlib.Path(inspect.getfile(module)).read_text('utf-8')


def assert_built_only_through_the_gated_factory(entrypoint: types.ModuleType) -> None:
    """``entrypoint`` calls ``gated_server`` and never ``grpc.aio.server``; the factory is the one bare constructor."""
    source = _source(entrypoint)
    if _BARE_SERVER in source:
        raise AssertionError(f'{entrypoint.__name__} builds a bare server, which the auth interceptor never gates')
    if 'gated_server(' not in source:
        raise AssertionError(f'{entrypoint.__name__} does not build its server through gated_server')
    if _source(interceptor_mod).count(_BARE_SERVER) != 1:
        raise AssertionError('the gated factory is no longer the one place a bare server is constructed')


def assert_no_module_constructs_a_server(package: types.ModuleType) -> None:
    """No module under ``package``, tests aside, constructs a bare ``grpc.aio`` server."""
    root = pathlib.Path(inspect.getfile(package)).parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob('*.py')
        if 'tests' not in path.parts and _BARE_SERVER in path.read_text('utf-8')
    ]
    if offenders:
        raise AssertionError(f'modules constructing a bare server: {offenders}')


def assert_servicer_reads_no_call_metadata(servicer_module: types.ModuleType) -> None:
    """``servicer_module`` never touches ``invocation_metadata``: the interceptor is the only reader."""
    accessed = {node.attr for node in ast.walk(ast.parse(_source(servicer_module))) if isinstance(node, ast.Attribute)}
    if 'invocation_metadata' in accessed:
        raise AssertionError(f'{servicer_module.__name__} reads the call metadata; only the interceptor may')
