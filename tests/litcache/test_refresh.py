"""Tests for the metadata-only refresh (`themis.litcache.refresh`).

Backed by a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture) and a
resolver stub: the refresh reads manifests and writes `metadata.pb` against a real
`google.cloud.storage.Bucket`; only the resolver ladder is replaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest
from google.cloud import storage as gcs

from themis.litcache import refresh, resolve, writer
from themis.litcache.models import litcache_pb2

_A = '1a000000-0000-4000-8000-000000000001'
_B = '2b000000-0000-4000-8000-000000000002'
_C = '3c000000-0000-4000-8000-000000000003'


def _metadata(pmid: str) -> bytes:
    metadata = litcache_pb2.PaperMetadata()
    metadata.pubmed.article.medline_citation.pmid.value = pmid
    return metadata.SerializeToString()


def _commit(
    bucket: gcs.Bucket, doc_id: str, *, pmid: str | None = None, doi: str | None = None, metadata: bytes | None
) -> None:
    """Write a committed paper: its manifest, and its `metadata.pb` unless `metadata` is None."""
    ids = litcache_pb2.ExternalIds()
    if pmid is not None:
        ids.pmid = pmid
    if doi is not None:
        ids.doi = doi
    manifest = litcache_pb2.Manifest(doc_id=doc_id, external_ids=ids)
    bucket.blob(writer.manifest_path(doc_id)).upload_from_string(manifest.SerializeToString())
    if metadata is not None:
        bucket.blob(writer.metadata_path(doc_id)).upload_from_string(metadata)


def _stub_resolver(records: Mapping[str, bytes | resolve.Outcome]) -> refresh.Resolver:
    """A resolver serving `records` (pmid -> metadata bytes, or a failure outcome to return as-is).

    A request it has no record for is absent, as the real resolver leaves an unknown paper.
    """

    async def resolver(requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.Outcome]:
        outcomes: dict[str, resolve.Outcome] = {}
        for r in requests:
            if r.pmid is None or r.pmid not in records:
                continue
            record = records[r.pmid]
            if isinstance(record, bytes):
                outcomes[r.claim_key] = resolve.ResolvedPaper(
                    metadata=record, external_ids=litcache_pb2.ExternalIds(pmid=r.pmid), publisher=None
                )
            else:
                outcomes[r.claim_key] = record
        return outcomes

    return resolver


def _run(bucket: gcs.Bucket, resolver: refresh.Resolver) -> refresh.RefreshReport:
    return asyncio.run(refresh.refresh(bucket, resolver))


def _read(bucket: gcs.Bucket, name: str) -> bytes | None:
    blob = bucket.blob(name)
    return blob.download_as_bytes() if blob.exists() else None


def test_plan_selects_only_committed_papers_without_metadata(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    _commit(gcs_bucket, _B, pmid='2', metadata=_metadata('2'))
    # An uncommitted paper (metadata but no manifest) is not a paper yet; ingestion owns it.
    gcs_bucket.blob(writer.metadata_path(_C)).upload_from_string(_metadata('3'))
    # A same-named object at a deeper depth is not a manifest, and a directory placeholder is nothing.
    gcs_bucket.blob(f'{writer.paper_dir(_C)}/sources/pdf/manifest.pb').upload_from_string(b'x')
    gcs_bucket.blob(f'{writer.paper_dir(_C)}/').upload_from_string(b'')

    found = refresh.plan(gcs_bucket)

    assert found.manifests == 2
    assert [r.claim_key for r in found.due] == [_A]
    assert found.failures == []


def test_plan_reports_an_unreadable_manifest(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob(writer.manifest_path(_A)).upload_from_string(b'\xff\xff not a manifest')

    found = refresh.plan(gcs_bucket)

    assert found.due == []
    assert [f.doc_id for f in found.failures] == [_A]
    assert 'unreadable' in found.failures[0].reason


def test_plan_limit_takes_the_first_due_in_doc_id_order(gcs_bucket: gcs.Bucket) -> None:
    for doc_id in (_C, _A, _B):
        _commit(gcs_bucket, doc_id, pmid=doc_id[0], metadata=None)

    found = refresh.plan(gcs_bucket, limit=2)

    assert [r.claim_key for r in found.due] == [_A, _B]


def test_plan_reports_a_manifest_whose_doc_id_disagrees_with_its_directory(gcs_bucket: gcs.Bucket) -> None:
    # The record would be written into _A's directory; a manifest naming _B must not steer it.
    stray = litcache_pb2.Manifest(doc_id=_B, external_ids=litcache_pb2.ExternalIds(pmid='1'))
    gcs_bucket.blob(writer.manifest_path(_A)).upload_from_string(stray.SerializeToString())

    found = refresh.plan(gcs_bucket)

    assert found.due == []
    assert [f.doc_id for f in found.failures] == [_A]
    assert 'disagrees' in found.failures[0].reason


def test_plan_reports_a_manifest_with_no_resolvable_id(gcs_bucket: gcs.Bucket) -> None:
    manifest = litcache_pb2.Manifest(doc_id=_A, external_ids=litcache_pb2.ExternalIds(pmcid='PMC1'))
    gcs_bucket.blob(writer.manifest_path(_A)).upload_from_string(manifest.SerializeToString())

    found = refresh.plan(gcs_bucket)

    assert found.due == []
    assert [f.doc_id for f in found.failures] == [_A]
    assert 'no pmid and no doi' in found.failures[0].reason


def test_plan_carries_a_doi_only_paper_as_a_doi_request(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, doi='10.1000/x', metadata=None)

    found = refresh.plan(gcs_bucket)

    assert found.due == [resolve.ResolveRequest(claim_key=_A, pmid=None, doi='10.1000/x')]


def test_plan_limit_is_not_consumed_by_a_paper_that_cannot_be_prepared(gcs_bucket: gcs.Bucket) -> None:
    # Failures sort first here; a limited run still reaches the papers it can refresh.
    gcs_bucket.blob(writer.manifest_path(_A)).upload_from_string(b'\xff\xff not a manifest')
    _commit(gcs_bucket, _B, pmid='2', metadata=None)

    found = refresh.plan(gcs_bucket, limit=1)

    assert [r.claim_key for r in found.due] == [_B]
    assert [f.doc_id for f in found.failures] == [_A]


def test_plan_rejects_a_non_positive_limit(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(ValueError, match='positive'):
        refresh.plan(gcs_bucket, limit=0)


def test_refresh_writes_the_missing_record_and_leaves_the_present_one(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    kept = _metadata('2')
    _commit(gcs_bucket, _B, pmid='2', metadata=kept)
    # The resolver holds a different record for B; a present metadata.pb is never asked about.
    resolver = _stub_resolver({'1': _metadata('1'), '2': _metadata('999')})

    report = _run(gcs_bucket, resolver)

    assert report.refreshed == [_A]
    assert report.failures == []
    assert _read(gcs_bucket, writer.metadata_path(_A)) == _metadata('1')
    assert _read(gcs_bucket, writer.metadata_path(_B)) == kept


def test_refresh_is_a_no_op_once_complete(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    resolver = _stub_resolver({'1': _metadata('1')})
    _run(gcs_bucket, resolver)

    report = _run(gcs_bucket, resolver)

    assert report.refreshed == []
    assert report.failures == []


def test_refresh_reports_a_resolver_miss_and_still_refreshes_the_rest(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    _commit(gcs_bucket, _B, pmid='2', metadata=None)

    report = _run(gcs_bucket, _stub_resolver({'1': _metadata('1')}))

    assert report.refreshed == [_A]
    assert [f.doc_id for f in report.failures] == [_B]
    assert 'unresolved' in report.failures[0].reason
    assert _read(gcs_bucket, writer.metadata_path(_B)) is None


def test_refresh_reports_a_record_the_store_cannot_take_without_writing_it(gcs_bucket: gcs.Bucket) -> None:
    """A record that exists but fails a precondition or its mirror is the resolver's failure, kept as one."""
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    _commit(gcs_bucket, _B, pmid='2', metadata=None)
    outcomes = {
        '1': resolve.RecordPreconditionFailure(reason='no article title'),
        '2': resolve.SchemaDriftFailure(reason='unknown key "foo"'),
    }

    report = _run(gcs_bucket, _stub_resolver(outcomes))

    assert report.refreshed == []
    assert {f.doc_id: f.reason for f in report.failures} == {
        _A: 'precondition failed: no article title',
        _B: 'schema drift: unknown key "foo"',
    }
    assert _read(gcs_bucket, writer.metadata_path(_A)) is None
    assert _read(gcs_bucket, writer.metadata_path(_B)) is None


def test_refresh_carries_plan_failures_into_its_report(gcs_bucket: gcs.Bucket) -> None:
    manifest = litcache_pb2.Manifest(doc_id=_A, external_ids=litcache_pb2.ExternalIds(pmcid='PMC1'))
    gcs_bucket.blob(writer.manifest_path(_A)).upload_from_string(manifest.SerializeToString())

    report = _run(gcs_bucket, _stub_resolver({}))

    assert report.refreshed == []
    assert [f.doc_id for f in report.failures] == [_A]


def test_refresh_gives_each_paper_sharing_an_id_its_own_record(gcs_bucket: gcs.Bucket) -> None:
    # Two doc_ids can hold the same pmid (an equivalence class); requests are keyed by doc_id
    # so each gets its own entry in the result.
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    _commit(gcs_bucket, _B, pmid='1', metadata=None)

    report = _run(gcs_bucket, _stub_resolver({'1': _metadata('1')}))

    assert report.refreshed == [_A, _B]
    assert _read(gcs_bucket, writer.metadata_path(_B)) == _metadata('1')


def test_refresh_carries_an_unreadable_manifest_into_its_failures(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob(writer.manifest_path(_A)).upload_from_string(b'\xff\xff not a manifest')
    _commit(gcs_bucket, _B, pmid='2', metadata=None)

    report = _run(gcs_bucket, _stub_resolver({'2': _metadata('2')}))

    assert report.refreshed == [_B]
    assert [f.doc_id for f in report.failures] == [_A]


def test_refresh_propagates_a_resolver_transport_failure_writing_nothing(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, pmid='1', metadata=None)

    async def failing(_requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.ResolvedPaper]:
        raise ConnectionError('efetch unreachable')

    with pytest.raises(ConnectionError):
        _run(gcs_bucket, failing)
    assert _read(gcs_bucket, writer.metadata_path(_A)) is None


def test_refresh_keeps_earlier_chunks_when_a_later_one_fails_and_resumes(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _A, pmid='1', metadata=None)
    _commit(gcs_bucket, _B, pmid='2', metadata=None)
    records = {'1': _metadata('1'), '2': _metadata('2')}
    serve = _stub_resolver(records)

    async def fail_on_b(requests: Sequence[resolve.ResolveRequest]) -> Mapping[str, resolve.Outcome]:
        if any(r.claim_key == _B for r in requests):
            raise ConnectionError('efetch unreachable')
        return await serve(requests)

    with pytest.raises(ConnectionError):
        asyncio.run(refresh.refresh(gcs_bucket, fail_on_b, chunk_size=1))
    assert _read(gcs_bucket, writer.metadata_path(_A)) == _metadata('1')
    assert _read(gcs_bucket, writer.metadata_path(_B)) is None

    report = _run(gcs_bucket, serve)  # the re-run picks up only what is still missing

    assert report.refreshed == [_B]
    assert report.failures == []
