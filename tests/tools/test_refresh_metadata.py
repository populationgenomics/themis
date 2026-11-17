"""Tests for the metadata-refresh driver (`tools.litcache.refresh_metadata`) through ``main()``."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from google.cloud import storage as gcs

from themis.litcache import metadata_fields, openalex, resolve, writer
from themis.litcache.models import litcache_pb2
from tools.litcache import refresh_metadata

_DOC = '1a000000-0000-4000-8000-000000000001'


@pytest.mark.parametrize(
    ('bad', 'expected_error'),
    [
        (['--bucket', 'elsewhere'], '--project'),
        (['--project', 'cpg-themis-test', '--limit', '0'], '--limit must be positive'),
        (['--project', 'cpg-themis-test', '--limit', '-3'], '--limit must be positive'),
    ],
)
def test_bad_arguments_are_rejected(bad: list[str], expected_error: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        refresh_metadata.main(bad)
    assert expected_error in capsys.readouterr().err


def _commit_without_metadata(bucket: gcs.Bucket, doc_id: str, pmid: str) -> None:
    manifest = litcache_pb2.Manifest(doc_id=doc_id, external_ids=litcache_pb2.ExternalIds(pmid=pmid))
    bucket.blob(writer.manifest_path(doc_id)).upload_from_string(manifest.SerializeToString())


def _metadata(pmid: str) -> bytes:
    source = litcache_pb2.MetadataSource()
    source.pubmed.article.medline_citation.pmid.value = pmid
    metadata = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
    return metadata.SerializeToString()


def test_default_bucket_follows_project_on_dry_run_and_writes_nothing(
    gcs_client: gcs.Client, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    bucket_name = f'{project}-fulltext'
    bucket = gcs_client.bucket(bucket_name)
    bucket.create()
    _commit_without_metadata(bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata.gcs, 'Client', lambda: gcs_client)

    def never_read_key(_project: str) -> openalex.ApiKey:
        raise AssertionError('a dry run must not read Secret Manager or resolve')

    monkeypatch.setattr(refresh_metadata.openalex_key, 'from_secret_manager', never_read_key)

    code = refresh_metadata.main(['--project', project, '--dry-run'])

    out = capsys.readouterr().out
    assert code == 0
    assert f'gs://{bucket_name}/papers/' in out
    assert _DOC in out
    assert not bucket.blob(writer.metadata_path(_DOC)).exists()


def test_explicit_bucket_overrides_project_default(
    gcs_client: gcs.Client, gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gcs_bucket.name is not None
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    _commit_without_metadata(gcs_bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata.gcs, 'Client', lambda: gcs_client)

    code = refresh_metadata.main(['--project', project, '--bucket', gcs_bucket.name, '--dry-run'])

    out = capsys.readouterr().out
    assert code == 0
    assert f'gs://{gcs_bucket.name}/papers/' in out
    assert _DOC in out


def test_dry_run_reports_a_paper_it_cannot_attempt_and_exits_non_zero(
    gcs_client: gcs.Client, gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gcs_bucket.name is not None
    manifest = litcache_pb2.Manifest(doc_id=_DOC, external_ids=litcache_pb2.ExternalIds(pmcid='PMC1'))
    gcs_bucket.blob(writer.manifest_path(_DOC)).upload_from_string(manifest.SerializeToString())
    monkeypatch.setattr(refresh_metadata.gcs, 'Client', lambda: gcs_client)

    code = refresh_metadata.main(['--project', 'cpg-themis-test', '--bucket', gcs_bucket.name, '--dry-run'])

    assert code == 1
    assert _DOC in capsys.readouterr().err
    assert not gcs_bucket.blob(writer.metadata_path(_DOC)).exists()


def test_a_resolver_miss_is_reported_and_exits_non_zero(
    gcs_client: gcs.Client, gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gcs_bucket.name is not None
    _commit_without_metadata(gcs_bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata.gcs, 'Client', lambda: gcs_client)
    monkeypatch.setattr(
        refresh_metadata.openalex_key, 'from_secret_manager', lambda proj: openalex.ApiKey(f'key-{proj}')
    )

    async def nothing(_requests: Sequence[resolve.ResolveRequest], **_kwargs: object) -> dict[str, resolve.Outcome]:
        return {}

    monkeypatch.setattr(refresh_metadata.resolve, 'resolve_batch', nothing)

    code = refresh_metadata.main(['--project', 'cpg-themis-test', '--bucket', gcs_bucket.name])

    assert code == 1
    assert _DOC in capsys.readouterr().err


def test_a_complete_refresh_exits_zero(
    gcs_client: gcs.Client, gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert gcs_bucket.name is not None
    _commit_without_metadata(gcs_bucket, _DOC, pmid='1')
    monkeypatch.setattr(refresh_metadata.gcs, 'Client', lambda: gcs_client)
    monkeypatch.setattr(
        refresh_metadata.openalex_key, 'from_secret_manager', lambda proj: openalex.ApiKey(f'key-{proj}')
    )
    seen_keys: list[str] = []

    async def one(
        requests: Sequence[resolve.ResolveRequest],
        *,
        openalex_api_key: openalex.ApiKey,
        **_kwargs: object,
    ) -> dict[str, resolve.Outcome]:
        seen_keys.append(openalex_api_key.value)
        return {
            r.join_key: resolve.ResolvedPaper(
                metadata=_metadata('1'), external_ids=litcache_pb2.ExternalIds(pmid='1'), publisher=None
            )
            for r in requests
        }

    monkeypatch.setattr(refresh_metadata.resolve, 'resolve_batch', one)

    code = refresh_metadata.main(['--project', 'cpg-themis-test', '--bucket', gcs_bucket.name])

    assert code == 0
    assert seen_keys == ['key-cpg-themis-test']
    assert gcs_bucket.blob(writer.metadata_path(_DOC)).download_as_bytes() == _metadata('1')
