"""The cspec interface's port and its adapters."""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import override

import httpx2

from themis.rpc import cspec_pb2
from themis.services.evidence import fixtures, provenance
from themis.services.evidence.upstreams import cspec

_SECTION = 'list_specifications'
SECTIONS = frozenset({_SECTION})


class CspecBackend(abc.ABC):
    """The CSpec Registry port: the seeded or fetched specifications, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def list_specifications(
        self, request: cspec_pb2.ListSpecificationsRequest
    ) -> cspec_pb2.ListSpecificationsResponse: ...


class FixtureBackend(CspecBackend):
    """In-memory backend answering from a seeded `{gene: specifications}` table."""

    def __init__(self, list_specifications: Mapping[str, cspec_pb2.ListSpecificationsResponse]) -> None:
        self._list_specifications = list_specifications

    @override
    async def list_specifications(
        self, request: cspec_pb2.ListSpecificationsRequest
    ) -> cspec_pb2.ListSpecificationsResponse:
        return fixtures.lookup(self._list_specifications, request.gene, kind='criteria_specification')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(fixtures.table(seeds, _SECTION, cspec_pb2.ListSpecificationsResponse, var_name=var_name))


class LiveBackend(CspecBackend):
    """The deployed backend, over the ClinGen CSpec Registry."""

    def __init__(self, http_client: httpx2.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def list_specifications(
        self, request: cspec_pb2.ListSpecificationsRequest
    ) -> cspec_pb2.ListSpecificationsResponse:
        """The registry traversal, stamped one Provenance per request issued."""
        at = provenance.utcnow()
        result = await cspec.fetch_criteria_specifications(request.gene, http_client=self._http_client)
        return cspec_pb2.ListSpecificationsResponse(
            specifications=result.specifications,
            coverage=result.coverage,
            raw=provenance.struct(result.raw),
            provenance=[provenance.provenance(query, at) for query in result.queries],
        )
