"""The mavedb interface's port and its adapters."""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import override

import httpx2

from themis.rpc import mavedb_pb2
from themis.services.evidence import fixtures, provenance
from themis.services.evidence.upstreams import allele_registry, mavedb

_SECTION = 'describe_variant'
SECTIONS = frozenset({_SECTION})


class MaveDbBackend(abc.ABC):
    """The MaveDB port: the seeded or fetched assay result, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def describe_variant(
        self, request: mavedb_pb2.DescribeVariantRequest
    ) -> mavedb_pb2.DescribeVariantResponse: ...


class FixtureBackend(MaveDbBackend):
    """In-memory backend answering from a seeded `{variant: assay result}` table."""

    def __init__(self, describe_variant: Mapping[str, mavedb_pb2.DescribeVariantResponse]) -> None:
        self._describe_variant = describe_variant

    @override
    async def describe_variant(self, request: mavedb_pb2.DescribeVariantRequest) -> mavedb_pb2.DescribeVariantResponse:
        return fixtures.lookup(self._describe_variant, request.variant, kind='mavedb')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(fixtures.table(seeds, _SECTION, mavedb_pb2.DescribeVariantResponse, var_name=var_name))


class LiveBackend(MaveDbBackend):
    """The deployed backend, over MaveDB and the ClinGen Allele Registry."""

    def __init__(self, http_client: httpx2.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def describe_variant(self, request: mavedb_pb2.DescribeVariantRequest) -> mavedb_pb2.DescribeVariantResponse:
        """The variant's ClinGen allele ids, then the MaveDB deposits keyed on them.

        The registry hop is a key resolution, not a second evidence source — MaveDB publishes no
        HGVS-keyed lookup — but it is an upstream that answered, so it is stamped like one.
        """
        at = provenance.utcnow()
        alleles = await allele_registry.fetch_clingen_allele_ids(request.variant, http_client=self._http_client)
        result = await mavedb.fetch_mavedb(alleles.allele_ids, http_client=self._http_client)
        response = mavedb_pb2.DescribeVariantResponse(
            acmg_criterion=result.acmg_criterion,
            acmg_strength=result.acmg_strength,
            raw=provenance.struct(result.raw),
            provenance=[provenance.provenance(alleles, at), provenance.provenance(result, at)],
        )
        if result.oddspath_ratio is not None:
            response.oddspath_ratio = result.oddspath_ratio
        if result.score is not None:
            response.score = result.score
        return response
