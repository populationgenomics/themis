"""Every literature rpc's contract admits somebody (rpc-authorization.md).

The interceptor derives an rpc's admission from its `admits_caller` option, and an rpc naming no caller
admits nobody. That is the right default for an rpc someone forgot to think about, and the wrong shipped
state for every rpc here, so it is checked off the compiled descriptor with no server running.
"""

from __future__ import annotations

from themis.rpc import literature_pb2, sandbox_options_pb2


def _callers(method_name: str) -> list[int]:
    method = literature_pb2.DESCRIPTOR.services_by_name['Literature'].methods_by_name[method_name]
    # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
    return list(method.GetOptions().Extensions[sandbox_options_pb2.admits_caller])  # pyright: ignore[reportArgumentType]


def test_every_literature_rpc_admits_somebody() -> None:
    methods = literature_pb2.DESCRIPTOR.services_by_name['Literature'].methods
    assert methods  # a service with no rpcs would pass vacuously
    admitting_nobody = [method.name for method in methods if not _callers(method.name)]
    assert not admitting_nobody, f'literature rpcs whose contract admits nobody: {admitting_nobody}'


def test_admits_caller_never_names_the_unspecified_member() -> None:
    # CALLER_UNSPECIFIED carries no account, so admitting it would admit nobody while reading as an admission.
    for method in literature_pb2.DESCRIPTOR.services_by_name['Literature'].methods:
        assert sandbox_options_pb2.CALLER_UNSPECIFIED not in _callers(method.name), method.name
