"""What every Cloud Run service shares: the URL it is called at, and the probe that admits a new instance.

Cloud Run serves every service at two URLs: a hashed one (`Service.uri`), and one built from the service's
name, the project number and the region. It admits an ID token minted for either. A service that checks the
audience itself (`themis.clients.auth.caller`) has to be told its URL in its own environment, and the hashed
one is an output of the service, so it cannot be. The built one is known before the service exists, so the
program hands that one to the service as its audience and to every caller as the URL to dial and mint for.

A new instance takes no request until its startup probe succeeds, and a request that scaled the service up
from zero waits for that. Cloud Run's defaults probe every 10 seconds with a 1-second timeout, so an instance
that is ready just after a failed attempt sits idle for most of 10 seconds, and on a server whose first
request is slower than the timeout (the web tier loads its proxy on its first request) a second period
passes too. Every service is probed each second instead, for a budget of attempts that covers its slowest
start. On the web tier an attempt that times out still runs to completion in the server, so the next one
finds the proxy loaded.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

# Cloud Run's floor for a probe's period; its timeout may not exceed the period, so it is 1 s too.
_PROBE_PERIOD_SECONDS = 1
# Covers a service's slowest start with a margin; a service whose start can run longer names its own.
_STARTUP_BUDGET_SECONDS = 60
# Cloud Run's cap on failure_threshold * period_seconds.
_MAX_STARTUP_BUDGET_SECONDS = 600


def service_url(*, name: str, project_number: str, region: str) -> str:
    """The URL Cloud Run serves the service `name` at, built from the name, project number and region."""
    return f'https://{name}-{project_number}.{region}.run.app'


def reported_url(service: gcp.cloudrunv2.Service, url: str) -> pulumi.Output[str]:
    """`url`, once Cloud Run reports it among the service's URLs, so a caller handed it depends on the service.

    Raises:
        ValueError: At deploy, if Cloud Run does not serve the service at `url`; every caller minting for it
            would be refused.
    """

    def check(urls: Sequence[str]) -> str:
        if url not in urls:
            raise ValueError(f'Cloud Run serves the service at {urls}, not at {url}')
        return url

    return service.urls.apply(check)


def http_startup_probe(
    path: str, *, budget_seconds: int = _STARTUP_BUDGET_SECONDS
) -> gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs:
    """A startup probe that GETs `path` on the container port each second, for up to `budget_seconds`.

    Raises:
        ValueError: If the budget is not a whole number of periods within Cloud Run's cap.
    """
    return gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
        http_get=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeHttpGetArgs(path=path),
        period_seconds=_PROBE_PERIOD_SECONDS,
        timeout_seconds=_PROBE_PERIOD_SECONDS,
        failure_threshold=_attempts(budget_seconds),
    )


def grpc_startup_probe(
    port: int, *, budget_seconds: int = _STARTUP_BUDGET_SECONDS
) -> gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs:
    """A startup probe that checks the grpc.health.v1 service on `port` each second, for up to `budget_seconds`.

    Raises:
        ValueError: If the budget is not a whole number of periods within Cloud Run's cap.
    """
    return gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
        grpc=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeGrpcArgs(port=port),
        period_seconds=_PROBE_PERIOD_SECONDS,
        timeout_seconds=_PROBE_PERIOD_SECONDS,
        failure_threshold=_attempts(budget_seconds),
    )


def _attempts(budget_seconds: int) -> int:
    """The failure threshold that spends `budget_seconds` on attempts one period apart."""
    if not 0 < budget_seconds <= _MAX_STARTUP_BUDGET_SECONDS or budget_seconds % _PROBE_PERIOD_SECONDS:
        raise ValueError(
            f'a startup budget is a whole number of {_PROBE_PERIOD_SECONDS} s periods up to '
            f'{_MAX_STARTUP_BUDGET_SECONDS} s, got {budget_seconds} s'
        )
    return budget_seconds // _PROBE_PERIOD_SECONDS
