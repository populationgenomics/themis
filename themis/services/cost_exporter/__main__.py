"""Cost-exporter entrypoint (`python -m themis.services.cost_exporter`): one run, then exit.

A Cloud Run Job execution on the exporter's schedule (`infra/themis_infra/cost.py`). The environment
carries the four Anthropic federation identifiers (plaintext ids, not credentials —
`docs/runbooks/claude-api-wif.md`, the cost exporter), the GCP project and region the gauges are written
under, and the run's deadline; all are read before any network call, and a missing one fails the run
naming it. The run scans every session, totals usage by agent, sets the four gauges per agent and the
heartbeat once, and flushes them through the Telemetry API. Any failure — a page that fails, a session
missing a field, the deadline, an export the API refuses — exits non-zero, writing nothing, and the
freshness alert on the heartbeat is what notices (`docs/design/cost-monitoring.md`, Design).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import os
from collections.abc import Mapping

import anthropic
from opentelemetry.sdk import metrics as sdk_metrics

from themis.clients import anthropic_wif
from themis.services.cost_exporter import env, export, gauges, sessions
from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import names

# The series' `job` label; the Cloud Run Job's name.
_SERVICE_NAME = 'themis-cost-exporter'
# The flush runs in a thread the run's deadline cannot interrupt, so this bounds it; the Job's timeout
# margin over the deadline (`infra/themis_infra/cost.py`) has to cover it.
_EXPORT_TIMEOUT = datetime.timedelta(seconds=30)

_DEADLINE_VAR = 'THEMIS_COST_EXPORTER_DEADLINE_SECONDS'

_logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Settings:
    """The run's configuration, read whole from the environment before any network call.

    Attributes:
        federation_rule_id: The `fdrl_…` rule pinning the exporter's runtime service account.
        organization_id: The Anthropic organization.
        service_account_id: The `svac_…` account the rule targets.
        workspace_id: The `wrkspc_…` workspace whose sessions are scanned.
        project: The GCP project the gauges are written to.
        location: The exporter's region — the `location` label of its series.
        deadline_seconds: The run's hard deadline, well under the schedule interval.
    """

    federation_rule_id: str
    organization_id: str
    service_account_id: str
    workspace_id: str
    project: str
    location: str
    deadline_seconds: int


def settings_from(environ: Mapping[str, str]) -> Settings:
    """Read the run's settings from `environ`; a missing or malformed value exits naming it."""
    return Settings(
        federation_rule_id=env.require(environ, 'ANTHROPIC_FEDERATION_RULE_ID'),
        organization_id=env.require(environ, 'ANTHROPIC_ORGANIZATION_ID'),
        service_account_id=env.require(environ, 'ANTHROPIC_SERVICE_ACCOUNT_ID'),
        workspace_id=env.require(environ, 'ANTHROPIC_WORKSPACE_ID'),
        project=env.require(environ, 'THEMIS_COST_EXPORTER_PROJECT'),
        location=env.require(environ, 'THEMIS_COST_EXPORTER_LOCATION'),
        deadline_seconds=env.positive_seconds(environ, _DEADLINE_VAR),
    )


def _meter_provider(settings: Settings) -> tuple[sdk_metrics.MeterProvider, telemetry_metrics.OneShotReader]:
    google_credentials, _ = telemetry_metrics.application_default_credentials()
    resource = telemetry_metrics.workload_resource(
        project=settings.project,
        service_name=_SERVICE_NAME,
        attributes={
            telemetry_metrics.CLOUD_REGION: settings.location,
            telemetry_metrics.SERVICE_INSTANCE_ID: settings.workspace_id,
        },
    )
    reader = telemetry_metrics.OneShotReader(
        telemetry_metrics.telemetry_api_exporter(google_credentials, timeout=_EXPORT_TIMEOUT)
    )
    return sdk_metrics.MeterProvider(metric_readers=[reader], resource=resource, shutdown_on_exit=False), reader


async def _run(settings: Settings) -> export.ExportReport:
    provider, reader = _meter_provider(settings)
    credentials = anthropic_wif.credentials(
        federation_rule_id=settings.federation_rule_id,
        organization_id=settings.organization_id,
        service_account_id=settings.service_account_id,
        workspace_id=settings.workspace_id,
    )
    try:
        # An explicit credential is total: the client reads no credential environment variable at all,
        # so a stray ANTHROPIC_API_KEY cannot reach it.
        async with (
            anthropic.AsyncAnthropic(credentials=credentials) as client,
            asyncio.timeout(settings.deadline_seconds),
        ):
            report = await export.run(
                sessions.AnthropicSessionUsage(client), gauges.TelemetryGauges(provider.get_meter(names.METER_NAME))
            )
        # The export is synchronous gRPC: off the event loop, bounded by its own timeout.
        await asyncio.to_thread(reader.flush, timeout=_EXPORT_TIMEOUT)
    finally:
        provider.shutdown()
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = settings_from(os.environ)
    try:
        report = asyncio.run(_run(settings))
    except TimeoutError:
        raise SystemExit(f'the run exceeded its {settings.deadline_seconds}s deadline') from None
    _logger.info('%d sessions observed', report.session_count)
    for agent, totals in sorted(report.totals.items()):
        _logger.info(
            '%s: %d cents cumulative list cost, %d tokens, %.0f active seconds, %d web searches',
            agent,
            totals.cents,
            sum(totals.tokens.values()),
            totals.active_seconds,
            totals.web_search_requests,
        )


if __name__ == '__main__':
    main()
