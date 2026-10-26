"""`metadata.pb` as a `SourcedMetadata`: its constraints on the way to bytes and back, and the `Metadata` read."""

from __future__ import annotations

import pytest
from pubmed_proto import pubmed_pb2

from themis.litcache import sourced_metadata
from themis.litcache.models import litcache_pb2


def _article() -> litcache_pb2.PubmedRecord:
    return litcache_pb2.PubmedRecord(article=pubmed_pb2.PubmedArticle())


def _sourced(source: litcache_pb2.MetadataSource, metadata: litcache_pb2.Metadata) -> litcache_pb2.SourcedMetadata:
    return litcache_pb2.SourcedMetadata(metadata=metadata, source=source)


@pytest.mark.parametrize(
    'sourced',
    [
        _sourced(
            litcache_pb2.MetadataSource(pubmed=_article()),
            litcache_pb2.Metadata(title='T', authors=[litcache_pb2.Metadata.Author(name='Jane Doe')], year='2020'),
        ),
        _sourced(
            litcache_pb2.MetadataSource(crossref_json='{"DOI":"10.1/x"}'),
            litcache_pb2.Metadata(title='T', container_title='J', abstract='A'),
        ),
        _sourced(litcache_pb2.MetadataSource(openalex_json='{"id":"W1"}'), litcache_pb2.Metadata()),
    ],
    ids=['pubmed', 'crossref', 'openalex'],
)
def test_bytes_round_trip_for_each_index(sourced: litcache_pb2.SourcedMetadata) -> None:
    assert sourced_metadata.parse(sourced_metadata.to_canonical_bytes(sourced)) == sourced


@pytest.mark.parametrize(
    ('sourced', 'reason'),
    [
        (litcache_pb2.SourcedMetadata(source=litcache_pb2.MetadataSource(pubmed=_article())), 'no metadata'),
        (litcache_pb2.SourcedMetadata(metadata=litcache_pb2.Metadata(title='T')), 'no source record'),
        (_sourced(litcache_pb2.MetadataSource(), litcache_pb2.Metadata()), 'no index record set'),
        (
            _sourced(litcache_pb2.MetadataSource(pubmed=litcache_pb2.PubmedRecord()), litcache_pb2.Metadata()),
            'neither of its kinds',
        ),
    ],
    ids=['no-metadata', 'no-source', 'no-index', 'pubmed-in-no-kind'],
)
def test_a_record_breaking_its_constraints_fails_both_ways(sourced: litcache_pb2.SourcedMetadata, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        sourced_metadata.to_canonical_bytes(sourced)
    with pytest.raises(ValueError, match=reason):
        sourced_metadata.parse(sourced.SerializeToString())


@pytest.mark.parametrize('text', ['[]', 'null', '"x"', '{', '{"n": NaN}'])
def test_a_json_record_that_is_not_a_json_object_fails_both_ways(text: str) -> None:
    sourced = _sourced(litcache_pb2.MetadataSource(crossref_json=text), litcache_pb2.Metadata())
    with pytest.raises(ValueError, match='crossref_json is not a JSON object'):
        sourced_metadata.to_canonical_bytes(sourced)
    with pytest.raises(ValueError, match='crossref_json is not a JSON object'):
        sourced_metadata.parse(sourced.SerializeToString())


def test_bytes_that_are_not_a_sourced_metadata_fail() -> None:
    with pytest.raises(ValueError, match='not a valid SourcedMetadata'):
        sourced_metadata.parse(b'\xff\xff\xff')


def test_a_consumer_reads_the_metadata_alone() -> None:
    sourced = _sourced(
        litcache_pb2.MetadataSource(openalex_json='{"id":"W1"}'), litcache_pb2.Metadata(title='A title', year='2024')
    )
    assert sourced_metadata.read_metadata(sourced.SerializeToString()) == litcache_pb2.Metadata(
        title='A title', year='2024'
    )


def test_a_refill_reads_a_record_whose_metadata_breaks_a_rule() -> None:
    sourced = _sourced(litcache_pb2.MetadataSource(openalex_json='{"id":"W1"}'), litcache_pb2.Metadata(year='24'))
    with pytest.raises(ValueError, match='not four digits'):
        sourced_metadata.parse(sourced.SerializeToString())
    assert sourced_metadata.parse_to_refill(sourced.SerializeToString()) == sourced


@pytest.mark.parametrize(
    ('sourced', 'reason'),
    [
        (litcache_pb2.SourcedMetadata(metadata=litcache_pb2.Metadata(title='T')), 'no source record'),
        (_sourced(litcache_pb2.MetadataSource(), litcache_pb2.Metadata()), 'no index record set'),
        (_sourced(litcache_pb2.MetadataSource(crossref_json='[]'), litcache_pb2.Metadata()), 'not a JSON object'),
    ],
    ids=['no-source', 'no-index', 'json-not-an-object'],
)
def test_a_refill_refuses_a_record_whose_source_breaks_its_constraints(
    sourced: litcache_pb2.SourcedMetadata, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        sourced_metadata.parse_to_refill(sourced.SerializeToString())
