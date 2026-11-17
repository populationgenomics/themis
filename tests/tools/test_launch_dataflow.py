"""Tests for the Dataflow launcher (`tools.litcache.launch_dataflow`) through ``main()``."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from apache_beam.options import pipeline_options
from google.cloud import storage as gcs

from themis.litcache import cloudsql, ingest_beam, openalex
from tools.litcache import launch_dataflow


class _SubmittedError(Exception):
    """Stops ``launch_dataflow.main`` at the ``ingest_beam.run_ingestion`` boundary."""


@dataclasses.dataclass(frozen=True)
class _CapturedSubmission:
    run_bucket: str
    conn_config: dict[str, str]
    options: dict[str, object]
    openalex_api_key: str


def _seed_source_and_scratch(
    gcs_client: gcs.Client, *, source_name: str, scratch_name: str
) -> tuple[gcs.Bucket, gcs.Bucket]:
    source = gcs_client.bucket(source_name)
    source.create()
    source.blob('ingest/10.1_seed.pdf').upload_from_string(b'%PDF-seed')
    scratch = gcs_client.bucket(scratch_name)
    scratch.create()
    return source, scratch


def _submit(monkeypatch: pytest.MonkeyPatch, gcs_client: gcs.Client, *argv: str) -> _CapturedSubmission:
    captured: list[_CapturedSubmission] = []

    def intercept_run(
        *,
        bucket_factory: ingest_beam.BucketFactory,
        conn_factory: ingest_beam.ConnFactory,
        options: pipeline_options.PipelineOptions,
        openalex_api_key_factory: ingest_beam.ApiKeyFactory,
        **_kwargs: object,
    ) -> ingest_beam.IngestionRun:
        run_bucket = bucket_factory().name
        assert run_bucket is not None
        assert isinstance(conn_factory, cloudsql.CrosswalkConnFactory)
        captured.append(
            _CapturedSubmission(
                run_bucket=run_bucket,
                conn_config=conn_factory.__getstate__(),
                options=options.get_all_options(),
                openalex_api_key=openalex_api_key_factory().value,
            )
        )
        raise _SubmittedError

    monkeypatch.setattr(launch_dataflow.gcs, 'Client', lambda: gcs_client)
    monkeypatch.setattr(
        launch_dataflow.openalex_key, 'from_secret_manager', lambda proj: openalex.ApiKey(f'key-for-{proj}')
    )
    monkeypatch.setattr(ingest_beam, 'run_ingestion', intercept_run)
    with pytest.raises(_SubmittedError):
        launch_dataflow.main(list(argv))
    (submission,) = captured
    return submission


@pytest.mark.parametrize(
    ('argv', 'expected_error'),
    [
        (['--sdk-image', 'img', '--limit', '1'], '--project'),
        (['--project', 'p', '--sdk-image', 'img'], '--limit is required unless --direct'),
        (['--project', 'p', '--sdk-image', 'img', '--limit', '0'], '--limit must be positive'),
        (
            ['--project', 'p', '--sdk-image', 'img', '--limit', '1', '--max-dead-letters', '-1'],
            '--max-dead-letters must not be negative',
        ),
        (
            ['--project', 'p', '--sdk-image', 'img', '--direct'],
            '--worker-url is required for a --direct run into the store',
        ),
    ],
)
def test_invalid_arguments_are_rejected(
    argv: list[str], expected_error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        launch_dataflow.main(argv)
    assert expected_error in capsys.readouterr().err


def test_every_derived_resource_and_dataflow_option_follows_the_project_and_default_region(
    monkeypatch: pytest.MonkeyPatch, gcs_client: gcs.Client
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    _, scratch = _seed_source_and_scratch(
        gcs_client,
        source_name=f'{project}-fulltext',
        scratch_name=f'{project}-litcache-scratch',
    )

    sub = _submit(
        monkeypatch,
        gcs_client,
        '--project',
        project,
        '--sdk-image',
        'img:v1',
        '--limit',
        '5',
        '--worker-machine-type',
        'n1-highmem-4',
    )

    assert scratch.blob('ingest/10.1_seed.pdf').exists()
    assert sub.run_bucket == f'{project}-litcache-scratch'
    assert sub.conn_config == {
        'connection_name': f'{project}:australia-southeast1:themis-sql',
        'database': 'themis',
        'iam_user': f'themis-ingest@{project}.iam',
    }
    assert sub.openalex_api_key == f'key-for-{project}'
    assert sub.options['project'] == project
    assert sub.options['region'] == 'australia-southeast1'
    assert sub.options['sdk_container_image'] == 'img:v1'
    assert sub.options['machine_type'] == 'n1-highmem-4'
    assert sub.options['temp_location'] == f'gs://{project}-litcache-scratch/dataflow/tmp'
    assert sub.options['staging_location'] == f'gs://{project}-litcache-scratch/dataflow/staging'
    assert sub.options['service_account_email'] == f'themis-ingest@{project}.iam.gserviceaccount.com'
    assert sub.options['subnetwork'] == (
        f'https://www.googleapis.com/compute/v1/projects/{project}'
        '/regions/australia-southeast1/subnetworks/themis-ingest'
    )


def test_explicit_region_reaches_sql_instance_subnet_and_dataflow_options(
    monkeypatch: pytest.MonkeyPatch, gcs_client: gcs.Client
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    _seed_source_and_scratch(
        gcs_client,
        source_name=f'{project}-fulltext',
        scratch_name=f'{project}-litcache-scratch',
    )

    sub = _submit(
        monkeypatch,
        gcs_client,
        '--project',
        project,
        '--region',
        'europe-west2',
        '--sdk-image',
        'img:v1',
        '--limit',
        '1',
    )

    assert sub.conn_config['connection_name'] == f'{project}:europe-west2:themis-sql'
    assert sub.options['region'] == 'europe-west2'
    assert sub.options['subnetwork'] == (
        f'https://www.googleapis.com/compute/v1/projects/{project}/regions/europe-west2/subnetworks/themis-ingest'
    )


def test_explicit_scratch_bucket_overrides_only_scratch_and_keeps_derived_source(
    monkeypatch: pytest.MonkeyPatch, gcs_client: gcs.Client
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    custom_scratch = f'custom-scratch-{uuid.uuid4().hex[:8]}'
    _, scratch = _seed_source_and_scratch(
        gcs_client,
        source_name=f'{project}-fulltext',
        scratch_name=custom_scratch,
    )

    sub = _submit(
        monkeypatch,
        gcs_client,
        '--project',
        project,
        '--scratch-bucket',
        custom_scratch,
        '--sdk-image',
        'img:v1',
        '--limit',
        '1',
    )

    assert scratch.blob('ingest/10.1_seed.pdf').exists()
    assert sub.run_bucket == custom_scratch
    assert sub.options['temp_location'] == f'gs://{custom_scratch}/dataflow/tmp'
    assert sub.options['staging_location'] == f'gs://{custom_scratch}/dataflow/staging'


def test_explicit_source_bucket_overrides_only_source_and_keeps_derived_scratch(
    monkeypatch: pytest.MonkeyPatch, gcs_client: gcs.Client
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    default_source = gcs_client.bucket(f'{project}-fulltext')
    default_source.create()
    default_source.blob('ingest/10.2_wrong.pdf').upload_from_string(b'%PDF-wrong')
    custom_source = f'custom-source-{uuid.uuid4().hex[:8]}'
    _, scratch = _seed_source_and_scratch(
        gcs_client,
        source_name=custom_source,
        scratch_name=f'{project}-litcache-scratch',
    )

    sub = _submit(
        monkeypatch,
        gcs_client,
        '--project',
        project,
        '--source-bucket',
        custom_source,
        '--sdk-image',
        'img:v1',
        '--limit',
        '1',
    )

    assert scratch.blob('ingest/10.1_seed.pdf').exists()
    assert not scratch.blob('ingest/10.2_wrong.pdf').exists()
    assert sub.run_bucket == f'{project}-litcache-scratch'
    assert sub.options['temp_location'] == f'gs://{project}-litcache-scratch/dataflow/tmp'
    assert sub.options['staging_location'] == f'gs://{project}-litcache-scratch/dataflow/staging'
