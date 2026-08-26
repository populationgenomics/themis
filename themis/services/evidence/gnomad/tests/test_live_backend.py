"""LiveBackend composition: the response `DescribeVariant` builds from a canned gnomAD result.

The upstream client function is replaced with a canned Result, so no test here touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from themis.rpc import gnomad_pb2
from themis.services.evidence.gnomad import backend as gnomad_backend
from themis.services.evidence.upstreams import gnomad


def _run[T](call: Callable[[gnomad_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient() as client:
            return await call(gnomad_backend.LiveBackend(client))

    return asyncio.run(run())


def test_gnomad_passthrough_maps_empty_cooccurrence_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_gnomad(_gnomad_id: str, dataset: str, **kwargs: object) -> gnomad.GnomadResult:
        captured['cooccurrence_with'] = kwargs['cooccurrence_with']
        return gnomad.GnomadResult(
            raw={'variant': {'af': 0.01}}, source='gnomAD GraphQL', dataset_versions=(dataset,), query='q'
        )

    monkeypatch.setattr(gnomad, 'fetch_gnomad', fake_gnomad)
    resp = _run(
        lambda be: be.describe_variant(gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4'))
    )
    assert captured['cooccurrence_with'] is None
    assert resp.raw.fields['variant'].struct_value.fields['af'].number_value == 0.01
    assert resp.provenance[0].dataset_versions == ['gnomad_r4']
