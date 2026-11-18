"""Tests for the one-off fill of every record's metadata fields (`tools.litcache.fill_metadata_fields`).

The pure `filled` runs offline; `run` and `verify` run against a fake-gcs-server bucket (Docker-gated via the shared
`gcs_bucket` fixture), so the generation-matched write-back is the production one.
"""

from __future__ import annotations

import contextlib
import urllib.parse
from collections.abc import Mapping
from typing import override

import requests
import requests.adapters
from google.auth import credentials
from google.cloud import storage as gcs
from google.protobuf import unknown_fields

from themis.litcache import sourced_metadata, upstream_record, writer
from themis.litcache.models import litcache_pb2
from tools.litcache import fill_metadata_fields

_DOC = '9f3a0000-0000-4000-8000-0000000000aa'
_OTHER = '9f3a0000-0000-4000-8000-0000000000bb'
_WORK: dict[str, object] = {
    'id': 'https://openalex.org/W1',
    'title': 'A preprint',
    'publication_year': 2024,
    'authorships': [{'author': {'display_name': 'Jane Doe'}}],
    'primary_location': {'source': {'display_name': 'bioRxiv'}},
}


def _stale(work: Mapping[str, object] = _WORK) -> litcache_pb2.SourcedMetadata:
    """An OpenAlex record whose metadata an older rule filled."""
    return litcache_pb2.SourcedMetadata(
        metadata=litcache_pb2.Metadata(title='An older title'),
        source=litcache_pb2.MetadataSource(openalex_json=upstream_record.to_text(work)),
    )


def _store(bucket: gcs.Bucket, sourced: litcache_pb2.SourcedMetadata, doc_id: str = _DOC) -> None:
    bucket.blob(writer.metadata_path(doc_id)).upload_from_string(sourced.SerializeToString())


def _load(bucket: gcs.Bucket, doc_id: str = _DOC) -> litcache_pb2.SourcedMetadata:
    return sourced_metadata.parse(bucket.blob(writer.metadata_path(doc_id)).download_as_bytes())


def test_a_record_with_no_metadata_is_filled_even_when_the_fill_is_empty() -> None:
    unfilled = litcache_pb2.SourcedMetadata(source=_stale({'id': 'https://openalex.org/W2', 'title': None}).source)
    result = fill_metadata_fields.filled(unfilled)
    assert result is not None
    assert result.HasField('metadata')


def test_a_dry_run_writes_nothing(gcs_bucket: gcs.Bucket) -> None:
    _store(gcs_bucket, _stale())
    report = fill_metadata_fields.run(gcs_bucket, dry_run=True, workers=2)
    assert (report.records, report.filled, report.failures) == (1, [_DOC], [])
    assert _load(gcs_bucket).metadata.title == 'An older title'


def test_a_run_fills_the_corpus_and_a_second_run_changes_nothing(gcs_bucket: gcs.Bucket) -> None:
    _store(gcs_bucket, _stale())
    assert fill_metadata_fields.verify(gcs_bucket, workers=2).failures

    first = fill_metadata_fields.run(gcs_bucket, dry_run=False, workers=2)
    assert (first.filled, first.failures) == ([_DOC], [])
    assert _load(gcs_bucket).metadata.title == 'A preprint'

    second = fill_metadata_fields.run(gcs_bucket, dry_run=False, workers=2)
    assert (second.filled, second.failures) == ([], [])
    assert fill_metadata_fields.verify(gcs_bucket, workers=2).failures == []


def test_a_record_that_cannot_be_filled_is_reported_and_left_alone(gcs_bucket: gcs.Bucket) -> None:
    unreadable = _stale({**_WORK, 'authorships': [{'author': {'display_name': ['Jane', 'Doe']}}]})
    _store(gcs_bucket, unreadable)
    _store(gcs_bucket, _stale(), doc_id=_OTHER)
    report = fill_metadata_fields.run(gcs_bucket, dry_run=False, workers=2)
    assert report.filled == [_OTHER]
    assert [f.doc_id for f in report.failures] == [_DOC]
    assert 'authorships[0].author.display_name' in report.failures[0].reason
    assert _load(gcs_bucket) == unreadable


def test_a_run_refills_metadata_that_breaks_a_rule_tightened_since_it_was_written(gcs_bucket: gcs.Bucket) -> None:
    stale = _stale()
    stale.metadata.year = '24'  # a value the current rule refuses
    _store(gcs_bucket, stale)
    report = fill_metadata_fields.run(gcs_bucket, dry_run=False, workers=2)
    assert (report.filled, report.failures) == ([_DOC], [])
    assert _load(gcs_bucket).metadata.year == '2024'


def test_a_fill_keeps_a_field_this_code_does_not_know(gcs_bucket: gcs.Bucket) -> None:
    # Field 99, a varint: what a newer writer's field looks like to this code, which must round-trip it untouched.
    unknown = b'\x98\x06\x01'
    gcs_bucket.blob(writer.metadata_path(_DOC)).upload_from_string(_stale().SerializeToString() + unknown)
    report = fill_metadata_fields.run(gcs_bucket, dry_run=False, workers=2)
    assert (report.filled, report.failures) == ([_DOC], [])
    written = _load(gcs_bucket)
    assert written.metadata.title == 'A preprint'
    assert [(f.field_number, f.data) for f in unknown_fields.UnknownFieldSet(written)] == [(99, 1)]


def test_verify_reports_a_record_filled_by_an_older_rule(gcs_bucket: gcs.Bucket) -> None:
    stale = fill_metadata_fields.filled(_stale())
    assert stale is not None
    stale.metadata.container_title = 'A container an older rule read'
    _store(gcs_bucket, stale)
    [failure] = fill_metadata_fields.verify(gcs_bucket, workers=2).failures
    assert 'differ from what the current rule fills' in failure.reason


def test_run_scans_only_paper_metadata_records(gcs_endpoint: str, gcs_bucket: gcs.Bucket) -> None:
    assert gcs_bucket.name is not None
    _store(gcs_bucket, _stale())
    gcs_bucket.blob('other/ignored.pb').upload_from_string(b'outside papers/ prefix')
    list_queries: list[dict[str, list[str]]] = []

    class _RecordListQuery(requests.adapters.HTTPAdapter):
        @override
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: dict[str, str] | None = None,
        ) -> requests.Response:
            parsed = urllib.parse.urlsplit(request.url or '')
            if request.method == 'GET' and parsed.path.endswith('/o'):
                list_queries.append(urllib.parse.parse_qs(parsed.query))
            return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

    session = requests.Session()
    session.mount('http://', _RecordListQuery())
    client = gcs.Client(
        project='themis-test',
        credentials=credentials.AnonymousCredentials(),
        client_options={'api_endpoint': gcs_endpoint},
        _http=session,
    )
    with contextlib.closing(session), contextlib.closing(client):
        report = fill_metadata_fields.run(client.bucket(gcs_bucket.name), dry_run=True, workers=1)

    assert report.records == 1
    assert list_queries
    assert list_queries[0]['prefix'] == ['papers/']
    assert list_queries[0]['matchGlob'] == ['papers/*/metadata.pb']


def test_a_record_read_without_a_generation_is_reported_and_never_written(
    gcs_endpoint: str, gcs_bucket: gcs.Bucket
) -> None:
    assert gcs_bucket.name is not None
    _store(gcs_bucket, _stale())
    stored = gcs_bucket.blob(writer.metadata_path(_DOC))
    stored.reload()
    initial_generation = stored.generation
    uploads: list[str] = []

    class _StripDownloadGeneration(requests.adapters.HTTPAdapter):
        @override
        def send(
            self,
            request: requests.PreparedRequest,
            stream: bool = False,
            timeout: float | tuple[float | None, float | None] | None = None,
            verify: bool | str = True,
            cert: str | tuple[str, str] | None = None,
            proxies: dict[str, str] | None = None,
        ) -> requests.Response:
            if request.method in ('POST', 'PUT'):
                uploads.append(request.url or '')
            response = super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)
            if request.method == 'GET' and '/download/' in (request.url or ''):
                response.headers.pop('X-Goog-Generation', None)
            return response

    session = requests.Session()
    session.mount('http://', _StripDownloadGeneration())
    client = gcs.Client(
        project='themis-test',
        credentials=credentials.AnonymousCredentials(),
        client_options={'api_endpoint': gcs_endpoint},
        _http=session,
    )
    with contextlib.closing(session), contextlib.closing(client):
        report = fill_metadata_fields.run(client.bucket(gcs_bucket.name), dry_run=False, workers=1)

    assert report.records == 1
    assert [f.doc_id for f in report.failures] == [_DOC]
    assert 'no generation' in report.failures[0].reason
    assert uploads == []
    stored.reload()
    assert stored.generation == initial_generation
    assert _load(gcs_bucket).metadata.title == 'An older title'
