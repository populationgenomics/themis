"""The splice interface's port and its adapters: whether a site is lost, and what is left if it is."""

from __future__ import annotations

import abc
import datetime
from collections.abc import Mapping
from typing import override

import httpx

from themis.evidence.models import evidence_pb2
from themis.rpc import splice_pb2
from themis.services.evidence import fixtures, provenance
from themis.services.evidence.splice import outcome as splice_outcome
from themis.services.evidence.upstreams import spliceai, transcript_sequence, transcript_structure

_DELTAS_SECTION = 'predict_deltas'
_OUTCOME_SECTION = 'predict_skip_outcome'
SECTIONS = frozenset({_DELTAS_SECTION, _OUTCOME_SECTION})

# PredictDeltas carries no genome build: the Broad SpliceAI/Pangolin assay is GRCh38-only.
_GRCH38 = 'GRCh38'


class SpliceBackend(abc.ABC):
    """The splice port: the seeded or fetched predictions, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def predict_deltas(self, request: splice_pb2.PredictDeltasRequest) -> splice_pb2.PredictDeltasResponse: ...

    @abc.abstractmethod
    async def predict_skip_outcome(
        self, request: splice_pb2.PredictSkipOutcomeRequest
    ) -> splice_pb2.PredictSkipOutcomeResponse: ...


def outcome_key(request: splice_pb2.PredictSkipOutcomeRequest) -> str:
    """The fixture key for one outcome query: the affected exon is named either way."""
    base = f'{request.transcript}:{request.genome_build}'
    match request.WhichOneof('affected'):
        case 'exon':
            return f'{base}:exon:{request.exon}'
        case _:
            return f'{base}:c:{request.cds_position}'


class FixtureBackend(SpliceBackend):
    """In-memory backend answering from the seeded per-rpc tables."""

    def __init__(
        self,
        predict_deltas: Mapping[str, splice_pb2.PredictDeltasResponse],
        predict_skip_outcome: Mapping[str, splice_pb2.PredictSkipOutcomeResponse],
    ) -> None:
        self._predict_deltas = predict_deltas
        self._predict_skip_outcome = predict_skip_outcome

    @override
    async def predict_deltas(self, request: splice_pb2.PredictDeltasRequest) -> splice_pb2.PredictDeltasResponse:
        return fixtures.lookup(self._predict_deltas, request.variant, kind='splice')

    @override
    async def predict_skip_outcome(
        self, request: splice_pb2.PredictSkipOutcomeRequest
    ) -> splice_pb2.PredictSkipOutcomeResponse:
        return fixtures.lookup(self._predict_skip_outcome, outcome_key(request), kind='splice_outcome')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(
        fixtures.table(seeds, _DELTAS_SECTION, splice_pb2.PredictDeltasResponse, var_name=var_name),
        fixtures.table(seeds, _OUTCOME_SECTION, splice_pb2.PredictSkipOutcomeResponse, var_name=var_name),
    )


class LiveBackend(SpliceBackend):
    """The deployed backend, over the Broad splice services and NCBI Nucleotide."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def predict_deltas(self, request: splice_pb2.PredictDeltasRequest) -> splice_pb2.PredictDeltasResponse:
        at = provenance.utcnow()
        result = await spliceai.fetch_splice(request.variant, _GRCH38, http_client=self._http_client)
        response = splice_pb2.PredictDeltasResponse(
            raw=provenance.struct(result.raw), provenance=_splice_provenance(result, at)
        )
        if result.spliceai_gain is not None:
            response.spliceai_gain = result.spliceai_gain
        if result.spliceai_loss is not None:
            response.spliceai_loss = result.spliceai_loss
        if result.pangolin_gain is not None:
            response.pangolin_gain = result.pangolin_gain
        if result.pangolin_loss is not None:
            response.pangolin_loss = result.pangolin_loss
        return response

    @override
    async def predict_skip_outcome(
        self, request: splice_pb2.PredictSkipOutcomeRequest
    ) -> splice_pb2.PredictSkipOutcomeResponse:
        at = provenance.utcnow()
        structure = await transcript_structure.fetch_transcript_structure(
            request.transcript, request.genome_build, http_client=self._http_client
        )
        sequence = await transcript_sequence.fetch_transcript_sequence(
            request.transcript, http_client=self._http_client
        )
        affected = _affected_exon(structure, request)
        return splice_pb2.PredictSkipOutcomeResponse(
            transcript=structure.transcript,
            affected_exon=affected,
            gene=structure.gene,
            genome_build=request.genome_build,
            skips=splice_outcome.predict_skips(structure, sequence, affected_exon=affected),
            raw=provenance.struct(
                {
                    'transcript_structure': structure.raw,
                    # The bases themselves are not echoed: 12 kb of sequence adds nothing the typed
                    # outcome does not already carry.
                    'transcript_sequence': {
                        'accession': sequence.accession,
                        'description': sequence.description,
                        'length': len(sequence.sequence),
                    },
                }
            ),
            provenance=[provenance.provenance(structure, at), provenance.provenance(sequence, at)],
        )


def _splice_provenance(result: spliceai.SpliceResult, at: datetime.datetime) -> list[evidence_pb2.Provenance]:
    """Split the adapter's one combined `SpliceAI-url | Pangolin-url` query into two Provenance."""
    spliceai_query, _, pangolin_query = result.query.partition(' | ')
    stamp = provenance.provenance(result, at).retrieved_at
    return [
        evidence_pb2.Provenance(
            source='Broad SpliceAI',
            dataset_versions=result.dataset_versions,
            query=spliceai_query,
            retrieved_at=stamp,
        ),
        evidence_pb2.Provenance(
            source='Broad Pangolin',
            dataset_versions=result.dataset_versions,
            query=pangolin_query or result.query,
            retrieved_at=stamp,
        ),
    ]


def _affected_exon(
    structure: transcript_structure.TranscriptStructureResult, request: splice_pb2.PredictSkipOutcomeRequest
) -> int:
    """The exon whose splice site is lost, resolving a c. position onto its exon."""
    if request.WhichOneof('affected') == 'exon':
        return request.exon
    return transcript_structure.position_in_transcript(structure, cds_position=request.cds_position).exon
