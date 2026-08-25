"""The upstream releases an answer rests on, lifted off a response and carried onto the tally.

Every evidence rpc stamps a provenance record per request it issued, and the release list on it is
neither optional nor partial (`themis/evidence/models/evidence.proto`): two facts joined across
releases nobody stated read as a disagreement about the gene rather than as a disagreement about
annotation. So each door lifts the releases off the response it read and hangs them on the value it
returns, and a `classify.Classification` carries the union of every one that reached the tally.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from themis.evidence.models import evidence_pb2


@dataclasses.dataclass(frozen=True)
class Release:
    """One release a fact rests on: the upstream that stated it, and the release it named.

    Attributes:
        source: The upstream, as its provenance record names it ("gnomAD GraphQL").
        dataset_version: One entry of that record's `dataset_versions`, verbatim ("gnomad_r4").
            The contract states a release as one opaque string and splits neither a dataset name nor
            a version number out of it, so neither is parsed here.

    What a release pins is which data a fact rests on, not when it was read. A live query's as-of
    time is its provenance record's `retrieved_at`, and it stays there: it belongs to the retrieval
    rather than to the release, and two calls to one source in a run would otherwise carry that
    source's release twice.
    """

    source: str
    dataset_version: str


def releases_of(records: Iterable[evidence_pb2.Provenance]) -> tuple[Release, ...]:
    """Every release the provenance records name, de-duplicated, in the order stated.

    Args:
        records: A response's `provenance` field.

    Returns:
        The releases, first occurrence first.

    Raises:
        ValueError: If the records name no release at all, or one of them names no source or carries
            an empty release. Every response carries provenance and its release list is never
            partial, so a gap is a malformed response rather than a fact resting on nothing.
    """
    releases: list[Release] = []
    for position, record in enumerate(records):
        if not record.source.strip():
            raise ValueError(f'provenance record {position} names no source')
        if not record.dataset_versions:
            raise ValueError(
                f'provenance record {position} ({record.source}) names no release; the release list on a '
                'response is neither optional nor partial'
            )
        for version in record.dataset_versions:
            if not version.strip():
                raise ValueError(f'provenance record {position} ({record.source}) carries an empty release')
            releases.append(Release(source=record.source, dataset_version=version))
    if not releases:
        raise ValueError('the response carries no provenance, so nothing states which releases its facts rest on')
    return _deduplicated(releases)


def union(*groups: Iterable[Release]) -> tuple[Release, ...]:
    """Merge several doors' releases, de-duplicated, in the order they were passed."""
    return _deduplicated([release for group in groups for release in group])


def _deduplicated(releases: Iterable[Release]) -> tuple[Release, ...]:
    return tuple(dict.fromkeys(releases))
