"""Record how a deploy ended: one point the deploy-failure alert reads.

The deploy workflow runs this as its last step, signed in as the environment's deploy account, so the alert reaches the
environment's alerts channel with no credential beyond the one the deploy already holds: once the deploy succeeded,
and when the run failed or was cancelled. The point is `themis_deploy_finished_timestamp_seconds`, the Unix time the
run ended, labelled `result`, on a series of its own per run: `instance` is the run's id and attempt. The alert holds
while a failure is newer than every success. The project is the credentials'; the region is passed, since nothing on a
GitHub runner places it.

    python -m themis.telemetry.deploy_outcome --region australia-southeast1 --run 123456789-1 --result failed
"""

from __future__ import annotations

import argparse
import datetime
import time
from collections.abc import Callable, Sequence

from opentelemetry.sdk import metrics as sdk_metrics

from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import names, operational_names, telemetry_api

# The series' `job` label.
SERVICE_NAME = 'themis-deploy'
_EXPORT_TIMEOUT = datetime.timedelta(seconds=30)


def record(
    *,
    project: str,
    region: str,
    run: str,
    result: operational_names.DeployResult,
    reader: telemetry_metrics.OneShotReader,
    clock: Callable[[], float] = time.time,
) -> None:
    """Write the run's outcome point through `reader`.

    Args:
        project: The project the point is written to.
        region: The environment's region, the series' location.
        run: The run's id and attempt, the series' instance.
        result: How the run ended.
        reader: The reader the point is flushed through.
        clock: Returns the Unix time the point is set to; the system clock outside tests.

    Raises:
        telemetry_metrics.ExportError: The Telemetry API did not accept the point.
    """
    resource = telemetry_metrics.workload_resource(
        project=project,
        service_name=SERVICE_NAME,
        attributes={telemetry_metrics.CLOUD_REGION: region, telemetry_metrics.SERVICE_INSTANCE_ID: run},
    )
    provider = sdk_metrics.MeterProvider(metric_readers=[reader], resource=resource, shutdown_on_exit=False)
    try:
        provider.get_meter(names.METER_NAME).create_gauge(
            operational_names.DEPLOY_FINISHED, description='Unix time a deploy run ended, by result.'
        ).set(clock(), {operational_names.RESULT_LABEL: result.value})
        reader.flush(timeout=_EXPORT_TIMEOUT)
    finally:
        provider.shutdown()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Record how a deploy ended: one point the deploy-failure alert reads.')
    parser.add_argument('--region', required=True, help="the environment's region, the series' location")
    parser.add_argument('--run', required=True, help="the run's id and attempt, the series' instance")
    parser.add_argument(
        '--result',
        required=True,
        type=operational_names.DeployResult,
        choices=list(operational_names.DeployResult),
        help='how the run ended',
    )
    args = parser.parse_args(argv)
    credentials, project = telemetry_api.application_default_credentials()
    reader = telemetry_metrics.OneShotReader(
        telemetry_metrics.telemetry_api_exporter(credentials, timeout=_EXPORT_TIMEOUT)
    )
    record(project=project, region=args.region, run=args.run, result=args.result, reader=reader)


if __name__ == '__main__':
    main()
