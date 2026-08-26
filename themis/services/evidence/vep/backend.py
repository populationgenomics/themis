"""The vep interface's port and its adapters."""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import cast, override

import httpx

from themis.evidence.models import evidence_pb2
from themis.rpc import vep_pb2
from themis.services.evidence import fixtures, provenance
from themis.services.evidence.upstreams import vep

_SECTION = 'annotate'
SECTIONS = frozenset({_SECTION})

# The rpc carries no genome build: VEP's MANE annotation is GRCh38-only.
_GRCH38 = 'GRCh38'


class VepBackend(abc.ABC):
    """The Ensembl VEP port: the seeded or fetched annotation, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def annotate(self, request: vep_pb2.AnnotateRequest) -> vep_pb2.AnnotateResponse: ...


class FixtureBackend(VepBackend):
    """In-memory backend answering from a seeded `{variant: annotation}` table."""

    def __init__(self, annotate: Mapping[str, vep_pb2.AnnotateResponse]) -> None:
        self._annotate = annotate

    @override
    async def annotate(self, request: vep_pb2.AnnotateRequest) -> vep_pb2.AnnotateResponse:
        return fixtures.lookup(self._annotate, request.variant, kind='vep')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(fixtures.table(seeds, _SECTION, vep_pb2.AnnotateResponse, var_name=var_name))


class LiveBackend(VepBackend):
    """The deployed backend, over the Ensembl VEP REST API."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def annotate(self, request: vep_pb2.AnnotateRequest) -> vep_pb2.AnnotateResponse:
        at = provenance.utcnow()
        annotation = await vep.fetch_vep(
            request.variant, list(request.predictors), _GRCH38, http_client=self._http_client
        )
        return vep_pb2.AnnotateResponse(
            most_severe_consequence=cast('evidence_pb2.Consequence', annotation.most_severe_consequence),
            raw=provenance.struct(annotation.raw),
            provenance=[provenance.provenance(annotation, at)],
        )
