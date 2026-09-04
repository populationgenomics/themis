"""Test the agent-exposure reader (``tools.schema.agent_exposed``).

Two layers: a pure collection-and-rendering step over a hand-built ``FileDescriptorSet`` (only the rpcs carrying
``agent_exposed`` are collected — absent *and* explicit-false are fail-closed — and an accessor exists for exactly
the services that have one), and an integration check over the real module compiled by ``buf`` that asserts the
worker-only surface stays off the hatch, so a broken buf→option read or a widened allowlist fails here rather than
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
    name: str, package: str, *, services: dict[str, dict[str, bool | None]]
) -> descriptor_pb2.FileDescriptorProto:
    """A file descriptor declaring `Request`/`Response` messages and services whose rpcs take and return them.

    Each rpc maps to its `agent_exposed` value: None leaves the option absent, else sets it explicitly.
    """
    file = descriptor_pb2.FileDescriptorProto(name=name, package=package, syntax='proto3')
    file.message_type.add(name='Request')
    file.message_type.add(name='Response')
    prefix = f'.{package}.' if package else '.'
    for service_name, methods in services.items():
        service = file.service.add(name=service_name)
        for method_name, exposed in methods.items():
            method = service.method.add(
                name=method_name, input_type=f'{prefix}Request', output_type=f'{prefix}Response'
            )
            if exposed is not None:
                # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
                method.options.Extensions[sandbox_options_pb2.agent_exposed] = exposed  # pyright: ignore[reportArgumentType]
    return file


def _image(*files: descriptor_pb2.FileDescriptorProto) -> bytes:
    return descriptor_pb2.FileDescriptorSet(file=files).SerializeToString()


def _methods(image: bytes) -> list[str]:
    return agent_exposed.exposed_methods(agent_exposed.marked_services(image))


def test_collects_only_marked_rpcs() -> None:
    image = _image(
        _file('themis/rpc/papers.proto', 'themis.rpc.papers', services={'Papers': {'Read': True, 'Locate': None}}),
        _file('themis/rpc/store.proto', 'themis.rpc.store', services={'Store': {'Get': None, 'Put': None}}),
    )
    assert _methods(image) == ['/themis.rpc.papers.Papers/Read']


def test_absent_option_is_fail_closed() -> None:
    image = _image(
        _file('themis/rpc/hello.proto', 'themis.rpc.hello', services={'Hello': {'Say': True}}),
        _file('themis/rpc/store.proto', 'themis.rpc.store', services={'Store': {'Get': None}}),
    )
    assert _methods(image) == ['/themis.rpc.hello.Hello/Say']


def test_explicit_false_is_fail_closed() -> None:
    image = _image(
        _file('themis/rpc/hello.proto', 'themis.rpc.hello', services={'Hello': {'Say': True}}),
        _file('themis/rpc/store.proto', 'themis.rpc.store', services={'Store': {'Get': False}}),
    )
    assert _methods(image) == ['/themis.rpc.hello.Hello/Say']


def test_marked_rpcs_across_services_sorted() -> None:
    image = _image(
        _file('themis/rpc/zeta.proto', 'themis.rpc.zeta', services={'Zeta': {'Do': True}}),
        _file(
            'themis/rpc/alpha.proto',
            'themis.rpc.alpha',
            services={'Alpha': {'Run': True, 'Cancel': True, 'Skip': None}},
        ),
    )
    assert _methods(image) == [
        '/themis.rpc.alpha.Alpha/Cancel',
        '/themis.rpc.alpha.Alpha/Run',
        '/themis.rpc.zeta.Zeta/Do',
    ]


def test_a_marked_service_keeps_its_rpcs_in_declaration_order() -> None:
    image = _image(
        _file('themis/rpc/alpha.proto', 'themis.rpc.alpha', services={'Alpha': {'Run': True, 'Cancel': True}})
    )
    (service,) = agent_exposed.marked_services(image)
    assert service.methods == ('Run', 'Cancel')


def test_marked_rpc_without_a_package_fails_loud() -> None:
    image = _image(_file('bad.proto', '', services={'Bad': {'Do': True}}))
    with pytest.raises(ValueError, match='no package'):
        agent_exposed.marked_services(image)


def test_two_services_sharing_an_accessor_name_fail_loud() -> None:
    image = _image(
        _file('themis/rpc/papers.proto', 'themis.rpc.papers', services={'Papers': {'Read': True}}),
        _file('other/papers.proto', 'other.papers', services={'Corpus': {'Read': True}}),
    )
    with pytest.raises(ValueError, match='collide'):
        agent_exposed.marked_services(image)


def test_two_files_of_one_name_in_different_packages_fail_loud() -> None:
    image = _image(
        _file('themis/rpc/search.proto', 'themis.rpc.papers', services={'Papers': {'Run': True}}),
        _file('other/search.proto', 'other.genes', services={'Genes': {'Run': True}}),
    )
    with pytest.raises(ValueError, match='one name from different packages'):
        agent_exposed.marked_services(image)


def test_an_image_with_no_marked_rpc_fails_loud() -> None:
    image = _image(_file('themis/rpc/store.proto', 'themis.rpc.store', services={'Store': {'Get': None}}))
    with pytest.raises(ValueError, match='no rpc carries agent_exposed'):
        agent_exposed.marked_services(image)


def test_an_accessor_hands_back_the_stub_over_the_hatch_channel() -> None:
    """One accessor per marked service, named for its package, returning the stub protoc names for the service."""
    image = _image(
        _file('themis/rpc/papers.proto', 'themis.rpc.papers', services={'Papers': {'Read': True, 'Locate': None}}),
        _file('themis/rpc/store.proto', 'themis.rpc.store', services={'Store': {'Get': None}}),
    )
    rendered = agent_exposed.render_guest_services(agent_exposed.marked_services(image))
    assert 'def papers() -> papers_pb2_grpc.PapersStub:' in rendered
    assert 'return papers_pb2_grpc.PapersStub(channel.to_hatch())' in rendered
    assert 'from themis.rpc import (\n    papers_pb2_grpc,\n)' in rendered
    assert 'Store' not in rendered
    assert 'store_pb2' not in rendered


@_needs_buf
def test_the_worker_only_services_are_never_exposed() -> None:
    """No store, auth or sheaf method reaches the allowlist, however many agent-facing rpcs carry the option.

    The set itself is not pinned: every agent-facing rpc legitimately adds to it. What cannot change is that the
    worker-only surface stays off the hatch — the working document and scratch are checkpointed by the worker,
    a session token is resolved by it, and the repository is published by its pre-receive hook after the hook's own
    checks — so none of it belongs to the guest, which speaks git to the mirror instead.
    """
    exposed = _methods(agent_exposed.build_image())
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
