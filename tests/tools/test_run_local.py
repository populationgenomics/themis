"""Tests for the local ingestion driver (`tools.litcache.run_local`) through ``main()``."""

from __future__ import annotations

import uuid

import pg8000.dbapi
import pytest
import testcontainers.postgres
from google.cloud import storage as gcs

from themis.litcache import crosswalk, ingest_beam, openalex, writer
from themis.litcache.models import litcache_pb2
from tools.litcache import run_local

_REQUIRED = ['--project', 'p', '--scratch-bucket', 'scratch', '--limit', '1']
_PG = ['--pg-host', 'localhost', '--pg-user', 'u', '--pg-database', 'd']
_EXISTING_DOC = '1a000000-0000-4000-8000-000000000001'


class _ReachedBoundaryError(Exception):
    """Stops ``run_local.main`` once the boundary under test is reached."""


def test_pg_host_requires_the_destructive_rebuild_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_local.main([*_REQUIRED, *_PG])
    assert '--pg-host requires --rebuild-crosswalk' in capsys.readouterr().err


def test_pg_host_with_rebuild_opt_in_stages_rebuilds_and_reaches_ingestion(
    monkeypatch: pytest.MonkeyPatch,
    gcs_client: gcs.Client,
    conn: pg8000.dbapi.Connection,
    postgres_container: testcontainers.postgres.PostgresContainer,
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    source = gcs_client.bucket(f'{project}-fulltext')
    source.create()
    source.blob('ingest/10.1_seed.pdf').upload_from_string(b'%PDF-seed')
    scratch_name = f'scratch-{uuid.uuid4().hex[:8]}'
    scratch = gcs_client.bucket(scratch_name)
    scratch.create()

    # Seed a stale crosswalk row (to prove it is wiped) and a committed manifest in scratch (to prove it is indexed).
    crosswalk.mint(conn, ['doi:10.9/stale'])
    committed = litcache_pb2.Manifest(
        doc_id=_EXISTING_DOC,
        claim_key='doi:10.1/kept',
        external_ids=litcache_pb2.ExternalIds(doi='10.1/kept'),
        equivalence=litcache_pb2.Equivalence(canonical_doc_id=_EXISTING_DOC),
    )
    scratch.blob(writer.manifest_path(_EXISTING_DOC)).upload_from_string(committed.SerializeToString())

    def intercept_run(
        *,
        bucket_factory: ingest_beam.BucketFactory,
        openalex_api_key_factory: ingest_beam.ApiKeyFactory,
        **_kwargs: object,
    ) -> ingest_beam.IngestionRun:
        assert bucket_factory().name == scratch_name
        assert openalex_api_key_factory().value == f'key-{project}'
        raise _ReachedBoundaryError

    monkeypatch.setattr(run_local.gcs, 'Client', lambda: gcs_client)
    monkeypatch.setattr(run_local.openalex_key, 'from_secret_manager', lambda proj: openalex.ApiKey(f'key-{proj}'))
    monkeypatch.setattr(ingest_beam, 'run_ingestion', intercept_run)

    with pytest.raises(_ReachedBoundaryError):
        run_local.main(
            [
                '--project',
                project,
                '--scratch-bucket',
                scratch_name,
                '--limit',
                '1',
                '--pg-host',
                postgres_container.get_container_host_ip(),
                '--pg-port',
                str(postgres_container.get_exposed_port(5432)),
                '--pg-user',
                'test',
                '--pg-password',
                'test',
                '--pg-database',
                'litcache',
                '--rebuild-crosswalk',
            ]
        )

    assert scratch.blob('ingest/10.1_seed.pdf').exists()
    assert crosswalk.lookup(conn, ['doi:10.9/stale', 'doi:10.1/kept']) == {'doi:10.1/kept': _EXISTING_DOC}


def test_the_throwaway_path_needs_no_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # No --pg-host: the container is created and dropped by this run, so `_parse_args` accepts it
    # without `--rebuild-crosswalk` and proceeds into bucket setup.
    def stop_at_gcs() -> gcs.Client:
        raise _ReachedBoundaryError

    monkeypatch.setattr(run_local.gcs, 'Client', stop_at_gcs)

    with pytest.raises(_ReachedBoundaryError):
        run_local.main(_REQUIRED)


@pytest.mark.parametrize(
    ('bad', 'expected_error'),
    [
        (['--limit', '0'], '--limit must be positive'),
        (['--direct-num-workers', '0'], '--direct-num-workers must be positive'),
        (['--max-dead-letters', '-1'], '--max-dead-letters must not be negative'),
    ],
)
def test_rejects_out_of_range_values(bad: list[str], expected_error: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_local.main([*_REQUIRED, *bad])
    assert expected_error in capsys.readouterr().err
