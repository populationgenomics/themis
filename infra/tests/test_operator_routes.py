"""An operator reaches every gRPC service a data-plane image serves, and only through that image's IAP backend."""

from __future__ import annotations

import importlib
import pathlib

import pytest

from themis.services import evidence as evidence_image
from themis_infra import capture, evidence, sheaf

_URL_MAP_TYPE = 'gcp:compute/uRLMap:URLMap'


def _declared_services(proto_module: str) -> set[str]:
    descriptor = importlib.import_module(f'themis.rpc.{proto_module}_pb2').DESCRIPTOR
    return {service.full_name for service in descriptor.services_by_name.values()}


def _evidence_image_services() -> set[str]:
    # Each interface package `<name>` serves the services of `themis/rpc/<name>.proto`; the image's own test holds
    # every such package to its registry (themis/services/evidence/tests/test_main.py).
    root = pathlib.Path(evidence_image.__path__[0])
    names = {child.name for child in root.iterdir() if (child / 'interface.py').is_file()}
    assert names, 'the evidence image has no interface; the check is vacuous'
    return {service for name in names for service in _declared_services(name)}


def test_the_evidence_routes_are_every_service_the_image_serves() -> None:
    assert set(evidence.GRPC_SERVICES) == _evidence_image_services()


def test_the_sheaf_routes_are_every_service_the_image_serves() -> None:
    assert set(sheaf.GRPC_SERVICES) == _declared_services('sheaf')


def _routed(program: capture.Capture) -> dict[str, set[str]]:
    """Each path pattern the URL maps route, with the backend services it reaches."""
    routed: dict[str, set[str]] = {}
    for url_map in capture.of_type(program, _URL_MAP_TYPE):
        for matcher in url_map.state.get('pathMatchers', []):
            for rule in matcher['pathRules']:
                for path in rule['paths']:
                    routed.setdefault(path, set()).add(rule['service'])
    return routed


@pytest.mark.parametrize(
    ('service', 'grpc_services'), [('themis-evidence', evidence.GRPC_SERVICES), ('themis-sheaf', sheaf.GRPC_SERVICES)]
)
def test_each_service_is_routed_to_its_own_iap_backend(
    program: capture.Capture, service: str, grpc_services: tuple[str, ...]
) -> None:
    backends = {b.id for b in capture.backends_fronting(program, service)}
    assert backends, f'{service}: no load-balancer backend'
    routed = _routed(program)
    for grpc_service in grpc_services:
        assert routed.get(f'/{grpc_service}/*') == backends, f'{grpc_service}: not routed to {service} alone'
    # Nothing else reaches the backend: a path routed to it is one of the service's own prefixes.
    stray = sorted(path for path, targets in routed.items() if targets & backends)
    assert stray == sorted(f'/{grpc_service}/*' for grpc_service in grpc_services)
