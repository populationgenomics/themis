"""The gnomad interface's port and its adapters.

The port method returns the generated proto response directly rather than a parallel domain
dataclass: the response is an upstream payload in `raw` plus a few typed fields, so a domain type
would be boilerplate re-wrapping with no invariant to enforce.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import override

import httpx2

from themis.rpc import gnomad_pb2
from themis.services.evidence import fixtures, provenance
from themis.services.evidence.upstreams import gnomad

_SECTION = 'describe_variant'
SECTIONS = frozenset({_SECTION})


class GnomadBackend(abc.ABC):
    """The gnomAD port: the seeded or fetched record, or `errors.UnknownVariantError`.

    Async because the servicer runs on `grpc.aio`: a real adapter offloads its upstream I/O rather
    than stalling the single event loop.
    """

    @abc.abstractmethod
    async def describe_variant(
        self, request: gnomad_pb2.DescribeVariantRequest
    ) -> gnomad_pb2.DescribeVariantResponse: ...


class FixtureBackend(GnomadBackend):
    """In-memory backend answering from a seeded `{gnomad_id: record}` table."""

    def __init__(self, describe_variant: Mapping[str, gnomad_pb2.DescribeVariantResponse]) -> None:
        self._describe_variant = describe_variant

    @override
    async def describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        return fixtures.lookup(self._describe_variant, request.gnomad_id, kind='gnomad')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(fixtures.table(seeds, _SECTION, gnomad_pb2.DescribeVariantResponse, var_name=var_name))


class LiveBackend(GnomadBackend):
    """The deployed backend, over the gnomAD GraphQL endpoint."""

    def __init__(self, http_client: httpx2.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        at = provenance.utcnow()
        result = await gnomad.fetch_gnomad(
            request.gnomad_id,
            request.dataset,
            http_client=self._http_client,
            cooccurrence_with=request.cooccurrence_with or None,
        )
        return gnomad_pb2.DescribeVariantResponse(
            raw=provenance.struct(result.raw), provenance=[provenance.provenance(result, at)]
        )
