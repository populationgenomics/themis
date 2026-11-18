"""Live `Splice` RPCs over in-process gRPC and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.rpc import splice_pb2, splice_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.splice import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_NF1_TRANSCRIPT = 'NM_001042492.3'
_NF1_STRUCTURE = json.loads((_FIXTURES / 'transcript_structure.json').read_bytes())
_NF1_FASTA = (_FIXTURES / 'transcript_sequence.fasta').read_text('utf-8')
_SPLICE_FIXTURE = json.loads((_FIXTURES / 'spliceai.json').read_bytes())
_VARIANT = '8-140300616-T-G'
_VV_METADATA = {
    'metadata': {
        'variantvalidator_version': '4.0.1.dev7+gbdab9c72f',
        'vvdb_version': 'vvdb_2025_3',
        'vvta_version': 'vvta_2025_02',
    }
}


def _run[T](
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
    call: Callable[[splice_pb2_grpc.SpliceAsyncStub], Awaitable[T]],
) -> T:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'live')

    async def run() -> T:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(handler)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await call(splice_pb2_grpc.SpliceStub(channel))

    return asyncio.run(run())


def test_predict_skip_outcome_takes_the_exon_from_a_cds_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chaining from a c. HGVS must not need the caller to work out the exon themselves."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/hello/':
            return httpx2.Response(200, json=_VV_METADATA)
        if request.url.host == 'rest.variantvalidator.org':
            expected_suffix = f'/{_NF1_TRANSCRIPT}/{_NF1_TRANSCRIPT}/refseq/GRCh38'
            assert request.url.path.endswith(expected_suffix), request.url.path
            return httpx2.Response(200, json=_NF1_STRUCTURE)
        assert request.url.path.endswith('/efetch.fcgi')
        assert request.url.params.get('id') == _NF1_TRANSCRIPT
        return httpx2.Response(200, text=_NF1_FASTA)

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.PredictSkipOutcome(
            splice_pb2.PredictSkipOutcomeRequest(transcript=_NF1_TRANSCRIPT, genome_build='GRCh38', cds_position=3496)
        ),
    )
    assert resp.gene == 'NF1'
    assert resp.affected_exon == 26
    assert resp.skips[0].nmd_predicted
    assert [p.source for p in resp.provenance] == [
        'VariantValidator gene2transcripts',
        'NCBI Nucleotide (E-utilities efetch)',
    ]
    sequence_record = resp.raw.fields['transcript_sequence'].struct_value
    assert sequence_record.fields['length'].number_value == 12373
    assert 'transcript_structure' in resp.raw


def test_predict_deltas_emits_dual_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params.get('variant') == f'chr{_VARIANT}'
        if '/spliceai/' in request.url.path:
            return httpx2.Response(200, json=_SPLICE_FIXTURE['spliceai'])
        assert '/pangolin/' in request.url.path
        return httpx2.Response(200, json=_SPLICE_FIXTURE['pangolin'])

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.PredictDeltas(splice_pb2.PredictDeltasRequest(variant=_VARIANT)),
    )
    assert resp.spliceai_gain == 0.04
    assert resp.spliceai_loss == 0.83
    assert resp.pangolin_gain == 0.29
    assert resp.pangolin_loss == 0.85
    assert [p.source for p in resp.provenance] == ['Broad SpliceAI', 'Broad Pangolin']
    assert 'spliceai-38' in resp.provenance[0].query
    assert 'pangolin-38' in resp.provenance[1].query


def test_predict_deltas_leaves_scores_absent_when_unscored(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params.get('variant') == f'chr{_VARIANT}'
        assert '/spliceai/' in request.url.path or '/pangolin/' in request.url.path
        return httpx2.Response(200, json={'scores': []})

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.PredictDeltas(splice_pb2.PredictDeltasRequest(variant=_VARIANT)),
    )
    for field in ('spliceai_gain', 'spliceai_loss', 'pangolin_gain', 'pangolin_loss'):
        assert not resp.HasField(field)
    assert [p.source for p in resp.provenance] == ['Broad SpliceAI', 'Broad Pangolin']
