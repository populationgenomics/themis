"""Record that a deploy failed: one point the deploy-failure alert reads.

The deploy workflow runs this as its last step when the run failed or was cancelled, signed in as the environment's
deploy account, so the alert reaches the environment's alerts channel with no credential beyond the one the deploy
already holds. The point is `themis_deploy_failed` = 1, on a series of its own per run: `instance` is the run's id and
attempt. The project is the credentials'; the region is passed, since nothing on a GitHub runner places it.

    python -m themis.telemetry.deploy_failure --region australia-southeast1 --run 123456789-1
"""

from __future__ import annotations

import argparse
import datetime
from collections.abc import Sequence

from opentelemetry.sdk import metrics as sdk_metrics

from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import names, operational_names, telemetry_api

# The series' `job` label.
SERVICE_NAME = 'themis-deploy'
_EXPORT_TIMEOUT = datetime.timedelta(seconds=30)


def record(*, project: str, region: str, run: str, reader: telemetry_metrics.OneShotReader) -> None:
    """Write the failure point through `reader`.

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
            operational_names.DEPLOY_FAILED, description='1 when a deploy run failed or was cancelled.'
        ).set(1)
        reader.flush(timeout=_EXPORT_TIMEOUT)
    finally:
        provider.shutdown()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description='Record that a deploy failed: one point the deploy-failure alert reads.'
    )
    parser.add_argument('--region', required=True, help="the environment's region, the series' location")
    parser.add_argument('--run', required=True, help="the run's id and attempt, the series' instance")
    args = parser.parse_args(argv)
    credentials, project = telemetry_api.application_default_credentials()
    reader = telemetry_metrics.OneShotReader(
        telemetry_metrics.telemetry_api_exporter(credentials, timeout=_EXPORT_TIMEOUT)
    )
    record(project=project, region=args.region, run=args.run, reader=reader)


if __name__ == '__main__':
    main()
