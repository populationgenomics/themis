"""Tests for the Dataflow launcher's target resolution (`tools.litcache.launch_dataflow`).

Argument parsing only — submitting a job needs Dataflow. What is worth pinning is that
naming a project moves *every* project-scoped resource with it: a flag that moved some of
them would send a run at one deployment's instance with another's bucket.
"""

from __future__ import annotations

import argparse

import pytest

from tools.litcache import launch_dataflow

_REQUIRED = ['--sdk-image', 'img', '--limit', '1']


def _parse(*extra: str) -> argparse.Namespace:
    return launch_dataflow._parse_args([*_REQUIRED, *extra])


def test_project_and_region_default_to_the_dev_deployment() -> None:
    args = _parse()
    assert args.target.project == launch_dataflow._DEFAULT_PROJECT
    assert args.target.region == launch_dataflow._DEFAULT_REGION


def test_every_derived_resource_follows_the_project() -> None:
    args = _parse('--project', 'cpg-themis-test')
    derived = [
        args.target.sql_connection_name,
        args.target.ingest_sa,
        args.target.ingest_db_user,
        args.target.subnetwork,
        args.scratch_bucket,
        args.source_bucket,
    ]
    assert all('cpg-themis-test' in value for value in derived)
    assert not any(launch_dataflow._DEFAULT_PROJECT in value for value in derived)


def test_region_reaches_the_instance_and_the_subnet() -> None:
    args = _parse('--region', 'europe-west2')
    assert 'europe-west2' in args.target.sql_connection_name
    assert 'europe-west2' in args.target.subnetwork
    assert launch_dataflow._DEFAULT_REGION not in args.target.subnetwork


@pytest.mark.parametrize(
    ('flag', 'overridden', 'still_derived'),
    [
        ('--scratch-bucket', 'scratch_bucket', 'source_bucket'),
        ('--source-bucket', 'source_bucket', 'scratch_bucket'),
    ],
)
def test_an_explicit_bucket_overrides_only_its_own_default(flag: str, overridden: str, still_derived: str) -> None:
    # Which attribute the flag lands on is the property worth pinning: swapping the two
    # would leave a staged run reading the seed from scratch and writing output to source.
    args = _parse('--project', 'cpg-themis-test', flag, 'someone-elses-bucket')
    assert getattr(args, overridden) == 'someone-elses-bucket'
    assert getattr(args, still_derived).startswith('cpg-themis-test-')


def test_dataflow_options_carry_the_target() -> None:
    # The mirror of the bug this guards: a flag that reaches everything except the options
    # would submit to one project while minting into another's Cloud SQL.
    target = launch_dataflow._Target(project='cpg-themis-test', region='europe-west2')
    options = launch_dataflow._dataflow_options(
        target=target, scratch_bucket='scratch', sdk_image='img', job_name='job', max_workers=1
    )
    resolved = options.get_all_options()

    assert resolved['project'] == 'cpg-themis-test'
    assert resolved['region'] == 'europe-west2'
    assert resolved['subnetwork'] == target.subnetwork
    assert resolved['service_account_email'] == target.ingest_sa
