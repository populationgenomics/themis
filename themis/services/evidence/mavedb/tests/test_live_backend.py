"""Live `MaveDb.DescribeVariant` over in-process gRPC and `httpx2.MockTransport`.

The registry answer is the recorded one for TP53 `NM_000546.6:c.524G>A`, and the MaveDB records are the
recorded ones for a BRCA1 deposit (`upstreams/tests/test_mavedb.py` states both recordings): the handler
lists that deposit under the registry answer's canonical allele, so the join across the two upstreams is
exercised without a second registry recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
import urllib.parse
from collections.abc import Mapping

import grpc
import grpc.aio
import httpx2
import pytest
from google.protobuf import json_format

from themis.rpc import mavedb_pb2, mavedb_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.mavedb import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations
from themis.testing import mavedb as fake_mavedb

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_ALLELE_FIXTURE = json.loads((_FIXTURES / 'allele_registry.json').read_bytes())
_MAVEDB_FIXTURE = json.loads((_FIXTURES / 'mavedb.json').read_bytes())
_VARIANT = 'NM_000546.6:c.524G>A'
_CANONICAL_ALLELE = 'CA000251'
_DEPOSIT = 'urn:mavedb:00000097-0-2#1864'
_DEPOSITED = {_CANONICAL_ALLELE: _MAVEDB_FIXTURE['measurements']['CA003252']}


def _describe_variant(
    measurements: Mapping[str, object], asked: list[str] | None = None
) -> mavedb_pb2.DescribeVariantResponse:
    """`DescribeVariant` on the recorded variant, with MaveDB listing `measurements` per allele id."""
    mavedb = fake_mavedb.handler(
        measurements=measurements, score_sets=_MAVEDB_FIXTURE['score_sets'], variants=_MAVEDB_FIXTURE['variants']
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            if request.url.params.get('hgvs') != _VARIANT:
                return httpx2.Response(400, json={'errorType': 'HgvsParsingError', 'description': 'unexpected hgvs'})
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if asked is not None:
            asked.append(request.url.path)
        return mavedb(request)

    async def run() -> mavedb_pb2.DescribeVariantResponse:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(handler)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await mavedb_pb2_grpc.MaveDbStub(channel).DescribeVariant(
                    mavedb_pb2.DescribeVariantRequest(variant=_VARIANT)
                )

    return asyncio.run(run())


def test_mavedb_asks_mavedb_about_the_alleles_the_variant_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry resolves the expression to allele ids; MaveDB is asked about those, not the text.

    Both upstreams answered, so both are stamped: a caller reading the score can see which allele the
    deposit was found under, and reproduce the join.
    """
    monkeypatch.setenv('THEMIS_MAVEDB_BACKEND', 'live')
    asked: list[str] = []

    resp = _describe_variant(_DEPOSITED, asked)

    assert fake_mavedb.measurements_path('CA000251') in asked
    assert fake_mavedb.measurements_path('PA106629') in asked
    assert [p.source for p in resp.provenance] == ['ClinGen Allele Registry', 'MaveDB']
    assert _VARIANT in urllib.parse.unquote(resp.provenance[0].query)
    assert 'CA000251' in resp.provenance[1].query
    assert resp.acmg_criterion == 'PS3'
    assert resp.acmg_strength == 'STRONG'
    assert resp.HasField('oddspath_ratio')
    assert resp.oddspath_ratio == 52.4
    assert resp.HasField('score')
    assert resp.score == -2.0082579967947303


def test_the_answer_repeats_nothing_clinvar_says_about_the_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """MaveDB's record carries ClinVar's assertions on its alleles and ClinVar's id for the variant.

    Both are the ClinVar service's to report, so neither may reach the answer, typed or passed through.
    """
    monkeypatch.setenv('THEMIS_MAVEDB_BACKEND', 'live')
    recorded = _MAVEDB_FIXTURE['variants'][_DEPOSIT]
    assertions = recorded['annotations']['ga4gh:VA.leXrIc1M8jXi3MANjehaoYVeuwAOp5Ol']['clinvar']
    assert assertions[0] == {
        'clinicalSignificance': 'Pathogenic',
        'clinicalReviewStatus': 'reviewed by expert panel',
        'clinvarVariationId': '55407',
        'clinvarAlleleId': '70074',
        'dbVersion': '01_2024',
    }
    assert assertions[1]['dbVersion'] == '01_2025'
    assert recorded['molecularRepresentation']['mappings'][3]['coding']['id'] == 'clinvar:55407'

    answer = json_format.MessageToJson(_describe_variant(_DEPOSITED))

    assert _DEPOSIT in answer
    assert 'Pathogenic' not in answer
    assert 'reviewed by expert panel' not in answer
    assert '55407' not in answer
    assert '70074' not in answer
    assert '01_2024' not in answer
    assert '01_2025' not in answer
    assert '"clinvar"' not in answer
    assert 'clinvar:' not in answer
    assert 'ncbi.nlm.nih.gov/clinvar' not in answer


def test_the_answer_states_no_variants_clinical_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A calibration lists its control variants with the clinical call that anchors it, this variant among them."""
    monkeypatch.setenv('THEMIS_MAVEDB_BACKEND', 'live')
    controls = _MAVEDB_FIXTURE['score_sets']['urn:mavedb:00000097-0-2']['scoreCalibrations'][0]['controls']
    assert controls[2] == {
        'variantUrn': _DEPOSIT,
        'clinicalStatus': 'pathogenic',
        'functionalClassificationId': 7528,
        'id': 3145,
        'recordType': 'SavedCalibrationControl',
        'creationDate': '2026-10-21',
        'modificationDate': '2026-10-21',
    }

    answer = json_format.MessageToJson(_describe_variant(_DEPOSITED))

    assert 'IGVF Coding Variant Focus Group' in answer
    assert '"controls"' not in answer
    assert 'clinicalStatus' not in answer
    assert 'SavedCalibrationControl' not in answer


def test_a_variant_mavedb_lists_no_measurement_for_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """MaveDB answered every allele with an empty list: no assay covers the variant, which is a settled answer."""
    monkeypatch.setenv('THEMIS_MAVEDB_BACKEND', 'live')

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe_variant({})

    assert caught.value.code() is grpc.StatusCode.NOT_FOUND
    assert 'no scored variant' in (caught.value.details() or '')
