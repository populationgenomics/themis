"""The internal data-plane services' shared egress VPC (docs/design/services.md).

The internal services (store, hello, and the genomics/compute services to come) resolve every request's
session through the auth service, which is internal-ingress. A Cloud Run service egresses to the public
internet by default, so a caller with no VPC attachment reaches auth's public front end and is rejected
before IAM — internal ingress admits only traffic that arrives over a same-project VPC. This module
provisions that VPC: the services attach to it with Direct VPC egress (all traffic, so the public-hostname
run.app call to auth traverses the VPC and is delivered internally), while the subnet's Private Google
Access keeps their Google-API traffic (GCS, logging) on the private path.

This VPC is deliberately not sealed. The egress boundary is the sandbox VPC (sandbox.py) — the untrusted
job reaches the outside world only through a service's controlled interface, never directly. The trusted
services on this network may reach the public internet (a genomics fetcher pulling ClinVar, say); a
service that needs it adds a Cloud NAT. store and hello need only auth, GCS, and the metadata server, so
no NAT is provisioned yet.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Distinct from the sandbox subnet (10.90/24) and the LB proxy-only subnet (10.91/24).
_SERVICES_SUBNET_CIDR = '10.92.0.0/24'


class ServicesNetwork(pulumi.ComponentResource):
    """The internal services' egress VPC and Private-Google-Access subnet.

    Attributes:
        network: The VPC the internal services attach to (Direct VPC egress).
        subnetwork: The regional subnet their instances draw egress addresses from.
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

        self.network = gcp.compute.Network(
            'themis-services',
            project=project,
            name='themis-services',
            auto_create_subnetworks=False,
            opts=child,
        )
        self.subnetwork = gcp.compute.Subnetwork(
            'themis-services',
            project=project,
            name='themis-services',
            region=region,
            network=self.network.id,
            ip_cidr_range=_SERVICES_SUBNET_CIDR,
            # No external addresses on the instances: Google APIs (and same-project internal-ingress
            # Cloud Run) reach over the private path, not a public IP.
            private_ip_google_access=True,
            opts=child,
        )
        self.register_outputs({'network': self.network.id, 'subnetwork': self.subnetwork.id})
