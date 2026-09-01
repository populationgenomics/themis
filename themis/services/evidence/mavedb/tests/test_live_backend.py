"""LiveBackend composition: the allele-id keying `DescribeVariant` runs the MaveDB lookup through.

Every upstream client function is replaced with a canned Result, so no test here touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

import httpx2
import pytest

from themis.rpc import mavedb_pb2
from themis.services.evidence.mavedb import backend as mavedb_backend
from themis.services.evidence.upstreams import allele_registry, mavedb


def _run[T](call: Callable[[mavedb_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient() as client:
            return await call(mavedb_backend.LiveBackend(client))

    return asyncio.run(run())


def test_mavedb_asks_mavedb_about_the_alleles_the_variant_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry resolves the expression to allele ids; MaveDB is asked about those, not the text.

    Both upstreams answered, so both are stamped: a caller reading the score can see which allele the
    deposit was found under, and reproduce the join.
    """
    captured: dict[str, object] = {}

    async def fake_fetch_clingen_allele_ids(hgvs: str, **_kwargs: object) -> allele_registry.ClinGenAlleleIds:
        captured['hgvs'] = hgvs
        return allele_registry.ClinGenAlleleIds(
            allele_ids=['CA000251', 'PA106629'],
            source='ClinGen Allele Registry',
            dataset_versions=(),
            query='registry-url',
        )

    async def fake_fetch_mavedb(allele_ids: Sequence[str], **_kwargs: object) -> mavedb.MavedbResult:
        captured['allele_ids'] = list(allele_ids)
        return mavedb.MavedbResult(
            oddspath_ratio=18.7,
            acmg_criterion='PS3',
            acmg_strength='STRONG',
            score=-2.5,
            raw={'urn': 'urn:mavedb:1'},
            source='MaveDB',
            dataset_versions=('urn:mavedb:1',),
            query='q',
        )

    monkeypatch.setattr(allele_registry, 'fetch_clingen_allele_ids', fake_fetch_clingen_allele_ids)
    monkeypatch.setattr(mavedb, 'fetch_mavedb', fake_fetch_mavedb)

    resp = _run(lambda be: be.describe_variant(mavedb_pb2.DescribeVariantRequest(variant='NP_000537.3:p.Arg175His')))
    assert captured['hgvs'] == 'NP_000537.3:p.Arg175His'
    assert captured['allele_ids'] == ['CA000251', 'PA106629']
    assert [p.source for p in resp.provenance] == ['ClinGen Allele Registry', 'MaveDB']
    assert resp.acmg_criterion == 'PS3'
    assert resp.acmg_strength == 'STRONG'
    assert resp.HasField('oddspath_ratio')
    assert resp.oddspath_ratio == 18.7
    assert resp.HasField('score')
    assert resp.score == -2.5
