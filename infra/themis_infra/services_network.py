"""The shared VPC every caller of an internal-ingress service reaches it over (docs/design/security.md).

The auth and hello services admit only internal traffic, and the evidence and sheaf services only internal
traffic and the load balancer's. A request counts as internal only if it arrives over a same-project VPC. A
`run.app` host resolves publicly to an address Cloud Run reaches over the internet, so a caller with no VPC
attachment reaches the service's public front end and is rejected before IAM. This module provisions that VPC.
The hello, sheaf and evidence services attach to it because they call auth, the web tier because it calls auth,
evidence and sheaf, and the sandbox Job because it calls hello, evidence and sheaf.

Each attaches with Direct VPC egress for private ranges only. An attached workload sends all of its DNS queries
to the VPC's DNS. For each internal service that is dialed over the network, the network has a private zone whose
DNS name is that service's run.app host, with one record at its apex: the addresses of private.googleapis.com.
That egress setting routes those addresses into the VPC and the subnet's Private Google Access delivers them, so
the callee counts the request as internal. A zone is authoritative for its host, so no lookup of that host, of
any record type, returns a public answer. Every other run.app host keeps its public answer, including another
project's Cloud Run service at its regional URL. Every other destination leaves by Cloud Run's own internet path
and never enters the VPC: Anthropic, the evidence service's upstream hosts, Google's APIs and the Cloud SQL
instance.

`ServicesNetwork.internal_url` creates a service's zone and record and returns the URL its callers are handed, so
a service whose URL comes from there has its zone, and each caller waits for the record.

The VPC has no NAT, so a workload that routed all traffic into it would have no path to a public host.

The network logs every DNS query it resolves for an attached workload, with the answer, to Cloud Logging.

The VPC is not an egress boundary. That boundary is the sandbox guest's empty network namespace (postern): the
untrusted code reaches the outside world only through the hatch. The trusted workloads on this network call
public hosts directly (sandbox-worker.md §"One trusted process, not two containers").
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

# Distinct from the sandbox subnet (10.90/24) and the LB proxy-only subnet (10.91/24).
_SERVICES_SUBNET_CIDR = '10.92.0.0/24'
# private.googleapis.com, which serves `*.run.app`. Google's other private-access range (restricted.googleapis.com)
# is for a VPC Service Controls perimeter, and one record must not mix the two.
_PRIVATE_GOOGLEAPIS = ipaddress.ip_network('199.36.153.8/30')
_DNS_TTL_SECONDS = 300
# What Cloud DNS accepts as a zone's name.
_ZONE_NAME = re.compile(r'[a-z]([a-z0-9-]{0,61}[a-z0-9])?')


def zone_name(service_name: str) -> str:
    """The name of the zone that answers the host of the Cloud Run service `service_name`.

    Raises:
        ValueError: If Cloud DNS would reject the name.
    """
    name = f'{service_name}-run-app'
    if _ZONE_NAME.fullmatch(name) is None:
        raise ValueError(
            f'{name!r} is not a Cloud DNS zone name: at most 63 lowercase letters, digits and hyphens, starting '
            'with a letter and ending with a letter or digit'
        )
    return name


def _host(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname
    if host is None:
        raise ValueError(f'{url!r} has no host')
    return host


def _reported_url(service: gcp.cloudrunv2.Service, url: str) -> pulumi.Output[str]:
    """`url`, once Cloud Run reports it among the service's URLs, so a caller handed it depends on the service.

    The value fails to resolve, with a `ValueError`, if Cloud Run does not serve the service at `url`; every caller
    minting for it would be refused.
    """

    def check(urls: Sequence[str]) -> str:
        if url not in urls:
            raise ValueError(f'Cloud Run serves the service at {urls}, not at {url}')
        return url

    return service.urls.apply(check)


class ServicesNetwork(pulumi.ComponentResource):
    """The VPC of the callers of internal-ingress services, and the DNS that keeps their calls on it.

    Attributes:
        network: The id of the VPC those callers attach to (Direct VPC egress).
        subnetwork: The id of the regional subnet their instances take their VPC addresses from.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:ServicesNetwork', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)
        self._project = project

        network = gcp.compute.Network(
            'themis-services',
            project=project,
            name='themis-services',
            auto_create_subnetworks=False,
            opts=child,
        )
        subnetwork = gcp.compute.Subnetwork(
            'themis-services',
            project=project,
            name='themis-services',
            region=region,
            network=network.id,
            ip_cidr_range=_SERVICES_SUBNET_CIDR,
            # Delivers a packet for private.googleapis.com from an instance that has no external address.
            private_ip_google_access=True,
            opts=child,
        )
        self.network = network.id
        self.subnetwork = subnetwork.id
        # A server policy that names no forwarding: it changes no answer and logs each query.
        gcp.dns.Policy(
            'themis-services',
            project=project,
            name='themis-services',
            description='Logs the DNS queries of the services network.',
            enable_logging=True,
            networks=[gcp.dns.PolicyNetworkArgs(network_url=network.id)],
            opts=child,
        )
        self.register_outputs({'network': self.network, 'subnetwork': self.subnetwork})

    def internal_url(self, service: gcp.cloudrunv2.Service, *, name: str, url: str) -> pulumi.Output[str]:
        """Create the zone and the record that answer an internal-ingress service's host, and return its URL.

        Both are registered as children of the network under the zone's name, so a second call for one service
        fails on a duplicate URN.

        Args:
            service: The Cloud Run service.
            name: The service's name, which its zone's name is built from.
            url: The URL `cloud_run.service_url` builds for the service.

        Returns:
            `url`, for the service's callers on this network. The value carries a dependency on the service and on
            the record: a workload handed it depends on both, so none of its revisions starts before the record
            answers. It fails to resolve, with a `ValueError` in the preview or update that reads the service's
            URLs, if Cloud Run does not serve the service at `url`; every caller minting for it would be refused.

        Raises:
            ValueError: See `zone_name`; or if `url` has no host.
        """
        resource_name = zone_name(name)
        apex = f'{_host(url)}.'
        child = pulumi.ResourceOptions(parent=self)
        zone = gcp.dns.ManagedZone(
            resource_name,
            project=self._project,
            name=resource_name,
            dns_name=apex,
            description=f'Answers the run.app host of {name} with private.googleapis.com on the services network.',
            visibility='private',
            private_visibility_config=gcp.dns.ManagedZonePrivateVisibilityConfigArgs(
                networks=[gcp.dns.ManagedZonePrivateVisibilityConfigNetworkArgs(network_url=self.network)]
            ),
            opts=child,
        )
        # No AAAA record: the subnet is IPv4-only, and private-ranges-only egress routes no IPv6 range into the VPC.
        record = gcp.dns.RecordSet(
            resource_name,
            project=self._project,
            managed_zone=zone.name,
            name=apex,
            type='A',
            ttl=_DNS_TTL_SECONDS,
            rrdatas=[str(address) for address in _PRIVATE_GOOGLEAPIS],
            opts=child,
        )
        # A revision that started before the record can keep the host's public answer for hours. The record's
        # name is an input, so the URL stays known in a preview that creates the record.
        return pulumi.Output.all(_reported_url(service, url), record.name).apply(lambda values: str(values[0]))
