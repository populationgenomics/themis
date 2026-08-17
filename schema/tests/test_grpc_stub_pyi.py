"""Every service proto's committed `_pb2_grpc.pyi` types the stub protoc leaves dynamic.

`--pyi_out` covers messages only, so a `_pb2_grpc.py` alone leaves a stub's methods as
`channel.unary_unary(...)` assignments a type-checker cannot see — and a call to an rpc the proto no
longer declares goes unnoticed (`docs/design/proto.md`, "Schema evolution"). The `.pyi` from
`protoc-gen-mypy_grpc` is what makes that a pyright error.

The freshness gate cannot catch this drifting: dropping the plugin from `regen.py` stops the files
being *re*generated, leaves the committed ones in place, and passes a `git diff` check. So the
expectation here is derived from the protos rather than pinned to today's stub set, and it is checked
both ways — a frozen stub that kept a retired rpc's attribute is the case the enforcement exists for.
"""

from __future__ import annotations

import ast
import pathlib
import shutil

import pytest
from google.protobuf import descriptor_pb2

from tools.schema import agent_exposed

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_needs_buf = pytest.mark.skipif(shutil.which('buf') is None, reason='buf not on PATH')


def _service_methods() -> dict[str, dict[str, list[str]]]:
    """`{proto path: {service name: [rpc names]}}` for every service-declaring proto in the module."""
    image = descriptor_pb2.FileDescriptorSet()
    image.ParseFromString(agent_exposed.build_image())
    return {
        file.name: {service.name: [method.name for method in service.method] for service in file.service}
        for file in image.file
        if file.service
    }


def _stub_attributes(pyi: pathlib.Path, class_name: str) -> set[str]:
    """The annotated attribute names declared on `class_name` in a stub `.pyi`."""
    tree = ast.parse(pyi.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f'{pyi} declares no class {class_name}')


@_needs_buf
def test_every_service_protos_stub_is_statically_typed() -> None:
    services = _service_methods()
    assert services, 'the module declares no services; the descriptor build is wrong'
    for proto, by_service in services.items():
        pyi = _REPO_ROOT / proto.replace('.proto', '_pb2_grpc.pyi')
        assert pyi.is_file(), f'{proto} declares a service but has no committed {pyi.name}'
        for service, methods in by_service.items():
            attributes = _stub_attributes(pyi, f'{service}Stub')
            missing = sorted(set(methods) - attributes)
            assert not missing, f'{pyi.name}: {service}Stub does not type {missing}'
            # The direction that matters: a frozen stub keeps a retired rpc's attribute, so a caller
            # of an rpc the proto no longer declares would still type-check.
            retired = sorted(attributes - set(methods))
            assert not retired, f'{pyi.name}: {service}Stub types {retired}, which the proto no longer declares'
