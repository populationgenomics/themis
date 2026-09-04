"""Test the agent-exposure surface generator (``tools.schema.agent_exposed``).

Two layers: a pure collection step over a hand-built ``FileDescriptorSet`` (only the methods of services in
an ``agent_exposed`` file are collected — absent *and* explicit-false are fail-closed — fully qualified and
sorted), and an integration check over the real module compiled by ``buf`` that asserts the exposed surface
is exactly the intended one, so a broken buf→option read or a widened allowlist fails here rather than
resting on review of the generated diff.
"""

from __future__ import annotations

import shutil

import pytest
from google.protobuf import descriptor_pb2

from themis.rpc import sandbox_options_pb2
from tools.schema import agent_exposed

_needs_buf = pytest.mark.skipif(shutil.which('buf') is None, reason='buf not on PATH')


def _file(
    name: str, package: str, *, exposed: bool | None, services: dict[str, list[str]]
) -> descriptor_pb2.FileDescriptorProto:
    """A file descriptor; `exposed` None leaves the option absent, else sets it to that explicit value."""
    file = descriptor_pb2.FileDescriptorProto(name=name, package=package, syntax='proto3')
    if exposed is not None:
        # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
        file.options.Extensions[sandbox_options_pb2.agent_exposed] = exposed  # pyright: ignore[reportArgumentType]
    for service_name, methods in services.items():
        service = file.service.add(name=service_name)
        for method in methods:
            service.method.add(name=method, input_type='.google.protobuf.Empty', output_type='.google.protobuf.Empty')
    return file


def _image(*files: descriptor_pb2.FileDescriptorProto) -> bytes:
    return descriptor_pb2.FileDescriptorSet(file=files).SerializeToString()


def test_collects_only_exposed_files() -> None:
    image = _image(
        _file('themis/rpc/hello.proto', 'themis.rpc.hello', exposed=True, services={'Hello': ['SayHello']}),
        _file('themis/rpc/store.proto', 'themis.rpc.store', exposed=None, services={'Store': ['Get', 'Put']}),
    )
    assert agent_exposed._exposed_methods(image) == ['/themis.rpc.hello.Hello/SayHello']


def test_absent_option_is_fail_closed() -> None:
    image = _image(_file('themis/rpc/store.proto', 'themis.rpc.store', exposed=None, services={'Store': ['Get']}))
    assert agent_exposed._exposed_methods(image) == []


def test_explicit_false_is_fail_closed() -> None:
    image = _image(_file('themis/rpc/store.proto', 'themis.rpc.store', exposed=False, services={'Store': ['Get']}))
    assert agent_exposed._exposed_methods(image) == []


def test_all_methods_of_all_services_in_an_exposed_file_sorted() -> None:
    image = _image(
        _file(
            'themis/rpc/multi.proto',
            'themis.rpc.multi',
            exposed=True,
            services={'Zeta': ['Do'], 'Alpha': ['Run', 'Cancel']},
        )
    )
    assert agent_exposed._exposed_methods(image) == [
        '/themis.rpc.multi.Alpha/Cancel',
        '/themis.rpc.multi.Alpha/Run',
        '/themis.rpc.multi.Zeta/Do',
    ]


def test_exposed_file_without_a_package_fails_loud() -> None:
    image = _image(_file('bad.proto', '', exposed=True, services={'Bad': ['Do']}))
    with pytest.raises(ValueError, match='no package'):
        agent_exposed._exposed_methods(image)


@_needs_buf
def test_the_worker_only_services_are_never_exposed() -> None:
    """No store, auth or sheaf method reaches the allowlist, however many agent-facing services carry the option.

    The set itself is not pinned: every agent-facing service legitimately adds to it. What cannot change is
    that the worker-only surface stays off the hatch — the working document and scratch are checkpointed by
    the worker, a session token is resolved by it, and the repository is published by its pre-receive hook
    after the hook's own checks — so none of it belongs to the guest, which speaks git to the mirror instead.
    """
    exposed = agent_exposed._exposed_methods(agent_exposed.build_image())
    assert exposed, 'nothing is exposed, so this would pass whatever the option did'
    worker_only = [
        m for m in exposed if m.startswith(('/themis.rpc.store.', '/themis.rpc.auth.', '/themis.rpc.sheaf.'))
    ]
    assert not worker_only


@_needs_buf
def test_service_files_excludes_the_options_proto() -> None:
    files = agent_exposed.service_files(agent_exposed.build_image())
    assert 'themis/rpc/hello.proto' in files
    assert 'themis/rpc/sandbox_options.proto' not in files
