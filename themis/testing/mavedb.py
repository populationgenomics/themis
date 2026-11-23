"""A fake of the MaveDB API, answering the reads the evidence adapter makes from records held in memory.

It answers as the live API did on 2026-11-23. A ClinGen allele's measurements are a list, empty for an allele it holds
nothing on and never a 404. A variant or a score set is read by URN, with a 404 for a URN it does not hold. Any other
request fails the test, so none can pass by asking something the fake does not model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx2

HOST = 'api.mavedb.org'
_API = '/api/v1'
_ALLELES = f'{_API}/clingen-alleles/'
_MEASUREMENTS = '/measurements'
# The path segment each kind of record is read under, and what MaveDB calls that kind in a 404.
_COLLECTIONS = {'score-sets': 'score set', 'variants': 'variant'}


def measurements_path(allele_id: str) -> str:
    """The path one ClinGen allele's measurements are read at."""
    return f'{_ALLELES}{allele_id}{_MEASUREMENTS}'


def record_path(collection: str, urn: str) -> str:
    """The path one record is read at, as `httpx2` reports it: the URN's percent-encoding decoded.

    Args:
        collection: `score-sets` or `variants`.
        urn: The record's URN.
    """
    return f'{_API}/{collection}/{urn}'


def handler(
    *,
    measurements: Mapping[str, object],
    score_sets: Mapping[str, object],
    variants: Mapping[str, object],
) -> Callable[[httpx2.Request], httpx2.Response]:
    """A transport handler answering for MaveDB.

    Each body is served as given, so a test of a response the adapter cannot read hands in that response.

    Args:
        measurements: The body answered for each ClinGen allele id, a list of measurements; an id not named lists none.
        score_sets: The score set record held under each URN.
        variants: The variant record held under each URN.
    """
    held = {'score-sets': score_sets, 'variants': variants}

    def answer(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.url.host != HOST or request.method != 'GET':
            raise AssertionError(f'the MaveDB fake does not model {request.method} {request.url.host}{path}')
        if path.startswith(_ALLELES) and path.endswith(_MEASUREMENTS):
            allele_id = path.removeprefix(_ALLELES).removesuffix(_MEASUREMENTS)
            return httpx2.Response(200, json=measurements.get(allele_id, []))
        for collection, kind in _COLLECTIONS.items():
            if path.startswith(record_path(collection, '')):
                urn = path.removeprefix(record_path(collection, ''))
                if urn not in held[collection]:
                    return httpx2.Response(404, json={'detail': f"{kind} with URN '{urn}' not found"})
                return httpx2.Response(200, json=held[collection][urn])
        raise AssertionError(f'the MaveDB fake does not model GET {path}')

    return answer
