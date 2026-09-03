"""Tests for the metadata-refresh driver (`tools.litcache.refresh_metadata`).

Argument guards, and the two behaviours the driver adds over `themis.litcache.refresh`:
`--dry-run` writes nothing, and a failure list is a non-zero exit. The refresh itself is
tested in `tests/litcache/test_refresh.py`; here the resolver is a stub and the bucket
the shared fake-gcs-server fixture.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from google.cloud import storage as gcs

from themis.litcache import resolve, writer
from themis.litcache.models import litcache_pb2
from tools.litcache import refresh_metadata

_DOC = '1a000000-0000-4000-8000-000000000001'


def test_bucket_follows_the_project() -> None:
    args = refresh_metadata._parse_args(['--project', 'cpg-themis-test'])
    assert args.bucket == 'cpg-themis-test-fulltext'


def test_an_explicit_bucket_is_kept() -> None:
    args = refresh_metadata._parse_args(['--bucket', 'elsewhere'])
    assert args.bucket == 'elsewhere'


@pytest.mark.parametrize('bad', [['--limit', '0'], ['--limit', '-3']])
def test_non_positive_limit_is_rejected(bad: list[str]) -> None:
    with pytest.raises(SystemExit):
        refresh_metadata._parse_args(bad)


def _commit_without_metadata(bucket: gcs.Bucket, doc_id: str, pmid: str) -> None:
    manifest = litcache_pb2.Manifest(doc_id=doc_id, external_ids=litcache_pb2.ExternalIds(pmid=pmid))
    bucket.blob(writer.manifest_path(doc_id)).upload_from_string(manifest.SerializeToString())


def _metadata(pmid: str) -> bytes:
    metadata = litcache_pb2.PaperMetadata()
    metadata.pubmed.article.medline_citation.pmid.value = pmid
    return metadata.SerializeToString()


def test_dry_run_lists_the_due_paper_and_writes_nothing(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit_without_metadata(gcs_bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata, '_open_bucket', lambda _name: gcs_bucket)

    async def never_called(_requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.ResolvedPaper]:
        raise AssertionError('a dry run must not resolve')

    monkeypatch.setattr(refresh_metadata, '_resolve_live', never_called)

    code = refresh_metadata.main(['--bucket', 'ignored', '--dry-run'])

    assert code == 0
    assert _DOC in capsys.readouterr().out
    assert not gcs_bucket.blob(writer.metadata_path(_DOC)).exists()


def test_dry_run_reports_a_paper_it_cannot_attempt_and_exits_non_zero(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = litcache_pb2.Manifest(doc_id=_DOC, external_ids=litcache_pb2.ExternalIds(pmcid='PMC1'))
    gcs_bucket.blob(writer.manifest_path(_DOC)).upload_from_string(manifest.SerializeToString())
    monkeypatch.setattr(refresh_metadata, '_open_bucket', lambda _name: gcs_bucket)

    code = refresh_metadata.main(['--bucket', 'ignored', '--dry-run'])

    assert code == 1
    assert _DOC in capsys.readouterr().err
    assert not gcs_bucket.blob(writer.metadata_path(_DOC)).exists()


def test_a_resolver_miss_is_reported_and_exits_non_zero(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit_without_metadata(gcs_bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata, '_open_bucket', lambda _name: gcs_bucket)

    async def nothing(_requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.ResolvedPaper]:
        return {}

    monkeypatch.setattr(refresh_metadata, '_resolve_live', nothing)

    code = refresh_metadata.main(['--bucket', 'ignored'])

    assert code == 1
    assert _DOC in capsys.readouterr().err


def test_a_complete_refresh_exits_zero(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    _commit_without_metadata(gcs_bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata, '_open_bucket', lambda _name: gcs_bucket)

    async def one(requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.ResolvedPaper]:
        return {
            r.claim_key: resolve.ResolvedPaper(
                metadata=_metadata('1'), external_ids=litcache_pb2.ExternalIds(pmid='1'), publisher=None
            )
            for r in requests
        }

    monkeypatch.setattr(refresh_metadata, '_resolve_live', one)

    code = refresh_metadata.main(['--bucket', 'ignored'])

    assert code == 0
    assert gcs_bucket.blob(writer.metadata_path(_DOC)).download_as_bytes() == _metadata('1')
