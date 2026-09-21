"""Every data-plane rpc's contract admits somebody (rpc-authorization.md).

The interceptor derives an rpc's admission from its `admits_caller` option, and an rpc naming no caller
admits nobody but the universal caller. That is the right default for an rpc someone forgot to think
about, and the wrong shipped state for every rpc here — and the one a behaviour test driven as the
universal caller cannot see, since that caller is admitted whatever the option says — so it is checked
off every compiled contract in `themis.rpc`, with no server running. `Auth.ResolveSession` is the one
exemption, and its contract says why: admission is derived through it, so it cannot be gated on one.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from google.protobuf import descriptor

import themis.rpc
from themis.rpc import auth_pb2, sandbox_options_pb2

_UNGATED = frozenset({auth_pb2.DESCRIPTOR.services_by_name['Auth'].methods_by_name['ResolveSession'].full_name})


def _methods() -> list[descriptor.MethodDescriptor]:
    """Every rpc of every service declared by a contract in `themis.rpc`."""
    methods = []
    for module_info in pkgutil.iter_modules(themis.rpc.__path__):
        if not module_info.name.endswith('_pb2'):
            continue
        module = importlib.import_module(f'{themis.rpc.__name__}.{module_info.name}')
        for service in module.DESCRIPTOR.services_by_name.values():
            methods.extend(service.methods)
    return methods


def _callers(method: descriptor.MethodDescriptor) -> list[int]:
    # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
    return list(method.GetOptions().Extensions[sandbox_options_pb2.admits_caller])  # pyright: ignore[reportArgumentType]


_METHODS = _methods()


def test_the_contracts_declare_rpcs() -> None:
    # The parametrised tests below pass vacuously over an empty discovery.
    assert _METHODS


_GATED = [method for method in _METHODS if method.full_name not in _UNGATED]


@pytest.mark.parametrize('method', _GATED, ids=[method.full_name for method in _GATED])
def test_every_gated_rpc_admits_somebody(method: descriptor.MethodDescriptor) -> None:
    assert _callers(method), f'{method.full_name}: its contract admits nobody'


@pytest.mark.parametrize('method', _METHODS, ids=[method.full_name for method in _METHODS])
def test_admits_caller_never_names_the_unspecified_member(method: descriptor.MethodDescriptor) -> None:
    # CALLER_UNSPECIFIED carries no account, so admitting it would admit nobody while reading as an admission.
    assert sandbox_options_pb2.CALLER_UNSPECIFIED not in _callers(method)
