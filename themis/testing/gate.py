"""Static checks that keep admission where rpc-authorization.md puts it, over an image's AST.

Two checks over parsed syntax trees: no module of the image constructs a bare ``grpc.aio`` server
(so admission is exercised behaviourally at the entrypoint without an un-gated server hiding in a
helper module), and a servicer never reads the call's metadata itself (so an identity header has
one reader, the interceptor).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import types


def _source(module: types.ModuleType) -> str:
    return pathlib.Path(inspect.getfile(module)).read_text('utf-8')


def _server_bindings(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Names bound in ``tree`` to ``grpc``, ``grpc.aio``, and ``grpc[.aio].server``."""
    grpc_names: set[str] = set()
    aio_names: set[str] = set()
    server_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'grpc':
                    grpc_names.add(alias.asname or 'grpc')
                elif alias.name == 'grpc.aio':
                    if alias.asname:
                        aio_names.add(alias.asname)
                    else:
                        grpc_names.add('grpc')
        elif isinstance(node, ast.ImportFrom) and node.module == 'grpc':
            for alias in node.names:
                if alias.name == 'aio':
                    aio_names.add(alias.asname or 'aio')
                elif alias.name == 'server':
                    server_names.add(alias.asname or 'server')
        elif isinstance(node, ast.ImportFrom) and node.module == 'grpc.aio':
            for alias in node.names:
                if alias.name == 'server':
                    server_names.add(alias.asname or 'server')
    return grpc_names, aio_names, server_names


def _constructs_a_bare_server(source: str, *, filename: str) -> bool:
    tree = ast.parse(source, filename=filename)
    grpc_names, aio_names, server_names = _server_bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in server_names:
            return True
        if not isinstance(node.func, ast.Attribute) or node.func.attr != 'server':
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and (receiver.id in aio_names or receiver.id in grpc_names):
            return True
        if (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == 'aio'
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id in grpc_names
        ):
            return True
    return False


def assert_no_module_constructs_a_server(package: types.ModuleType) -> None:
    """No module under ``package``, tests aside, constructs a bare ``grpc`` or ``grpc.aio`` server."""
    root = pathlib.Path(inspect.getfile(package)).parent
    offenders = [
        str(path.relative_to(root))
        for path in sorted(root.rglob('*.py'))
        if 'tests' not in path.relative_to(root).parts
        and _constructs_a_bare_server(path.read_text('utf-8'), filename=str(path))
    ]
    if offenders:
        raise AssertionError(f'modules constructing a bare server: {offenders}')


def assert_servicer_reads_no_call_metadata(servicer_module: types.ModuleType) -> None:
    """``servicer_module`` never touches ``invocation_metadata``: the interceptor is the only reader."""
    accessed = {node.attr for node in ast.walk(ast.parse(_source(servicer_module))) if isinstance(node, ast.Attribute)}
    if 'invocation_metadata' in accessed:
        raise AssertionError(f'{servicer_module.__name__} reads the call metadata; only the interceptor may')
