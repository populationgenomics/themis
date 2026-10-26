"""The URL a Cloud Run service is called at, and the ID-token audience its callers mint for.

Cloud Run serves every service at two URLs: a hashed one (`Service.uri`), and one built from the service's
name, the project number and the region. It admits an ID token minted for either. A service that checks the
audience itself (`themis.clients.auth.caller`) has to be told its URL in its own environment, and the hashed
one is an output of the service, so it cannot be. The built one is known before the service exists, so the
program hands that one to the service as its audience and to every caller as the URL to dial and mint for.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp


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
