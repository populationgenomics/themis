"""Two properties of the evidence image that keep admission where the design puts it (rpc-authorization.md).

The server is built through `interceptor.gated_server` and no other way, so every rpc of every interface
is behind the auth interceptor without anyone remembering to add it. And the literature servicer — the one
whose contract names callers — never reads the call's metadata itself, so an identity header has exactly
one reader, the interceptor, and that reader does not honour one. Both are read off the source, the way
the grants tests read the program.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from themis.clients.auth import interceptor as interceptor_mod
from themis.services import evidence
from themis.services.evidence.literature import servicer as literature_servicer

_EVIDENCE = pathlib.Path(inspect.getfile(evidence)).parent


def _source(module_path: pathlib.Path) -> str:
    return module_path.read_text('utf-8')


def _attribute_accesses(source: str) -> set[str]:
    return {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}


def test_the_evidence_server_is_built_only_through_the_gated_factory() -> None:
    entrypoint = _source(_EVIDENCE / '__main__.py')
    assert 'grpc.aio.server(' not in entrypoint, 'a server built without the auth interceptor gates nothing'
    assert 'gated_server(' in entrypoint
    # The factory is the one place a bare server is constructed.
    assert _source(pathlib.Path(inspect.getfile(interceptor_mod))).count('grpc.aio.server(') == 1


def test_no_evidence_module_constructs_a_server() -> None:
    offenders = [
        str(path.relative_to(_EVIDENCE))
        for path in _EVIDENCE.rglob('*.py')
        if 'tests' not in path.parts and 'grpc.aio.server(' in _source(path)
    ]
    assert offenders == [], offenders


def test_the_literature_servicer_reads_no_call_metadata() -> None:
    accessed = _attribute_accesses(_source(pathlib.Path(inspect.getfile(literature_servicer))))
    assert 'invocation_metadata' not in accessed
