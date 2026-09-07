"""Cost-exporter entrypoint (`python -m themis.services.cost_exporter`): one run, then exit.

A Cloud Run Job execution on the exporter's schedule (`infra/themis_infra/cost.py`). The environment
carries the four Anthropic federation identifiers (plaintext ids, not credentials —
`docs/runbooks/claude-api-wif.md`, the cost exporter), the GCP project and region the gauge is written
under, and the run's deadline; all are read before any network call, and a missing one fails the run
naming it. The run scans every session, totals list cost by agent and writes the gauge once. Any failure
— a page that fails, a session missing a field, the deadline — exits non-zero, and the freshness alert
on the metric is what notices (`docs/design/cost-monitoring.md`, Design).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import functools
import logging
import os
from collections.abc import Mapping

import anthropic
from google.cloud import monitoring_v3

from themis.clients import anthropic_wif
from themis.services.cost_exporter import export, gauge, sessions

# The `generic_task` the gauge's series belong to (gauge.GaugeTarget); fixed for the exporter.
_NAMESPACE = 'themis'
_JOB = 'cost-exporter'

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
        project: The GCP project the gauge is written to.
        location: The exporter's region — the `generic_task` location of its series.
        deadline_seconds: The run's hard deadline, well under the schedule interval.
    """

    federation_rule_id: str
    organization_id: str
    service_account_id: str
    workspace_id: str
    project: str
    location: str
    deadline_seconds: int


def _require(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def settings_from(environ: Mapping[str, str]) -> Settings:
    """Read the run's settings from `environ`; a missing or malformed value exits naming it."""
    deadline = _require(environ, _DEADLINE_VAR)
    if not deadline.isdigit() or int(deadline) == 0:
        raise SystemExit(f'{_DEADLINE_VAR} must be a positive whole number of seconds, got {deadline!r}')
    return Settings(
        federation_rule_id=_require(environ, 'ANTHROPIC_FEDERATION_RULE_ID'),
        organization_id=_require(environ, 'ANTHROPIC_ORGANIZATION_ID'),
        service_account_id=_require(environ, 'ANTHROPIC_SERVICE_ACCOUNT_ID'),
        workspace_id=_require(environ, 'ANTHROPIC_WORKSPACE_ID'),
        project=_require(environ, 'THEMIS_COST_EXPORTER_PROJECT'),
        location=_require(environ, 'THEMIS_COST_EXPORTER_LOCATION'),
        deadline_seconds=int(deadline),
    )


async def _run(settings: Settings) -> export.ExportReport:
    credentials = anthropic_wif.credentials(
        federation_rule_id=settings.federation_rule_id,
        organization_id=settings.organization_id,
        service_account_id=settings.service_account_id,
        workspace_id=settings.workspace_id,
    )
    target = gauge.GaugeTarget(
        project=settings.project,
        location=settings.location,
        namespace=_NAMESPACE,
        job=_JOB,
        task_id=settings.workspace_id,
    )
    # An explicit credential is total: the client reads no credential environment variable at all, so
    # a stray ANTHROPIC_API_KEY cannot reach it.
    async with anthropic.AsyncAnthropic(credentials=credentials) as client, asyncio.timeout(settings.deadline_seconds):
        with monitoring_v3.MetricServiceClient() as metrics:
            return await export.run(
                sessions.AnthropicSessionCosts(client),
                gauge.CloudMonitoringGauge(metrics, target),
                now=functools.partial(datetime.datetime.now, datetime.UTC),
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = settings_from(os.environ)
    try:
        report = asyncio.run(_run(settings))
    except TimeoutError:
        raise SystemExit(f'the run exceeded its {settings.deadline_seconds}s deadline') from None
    _logger.info('%d sessions observed at %s', report.session_count, report.at.isoformat())
    for agent, cents in sorted(report.totals.items()):
        _logger.info('%s: %d cents cumulative list cost', agent, cents)


if __name__ == '__main__':
    main()
