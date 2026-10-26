"""The shared egress VPC of the web tier and the data-plane services (docs/design/services.md).

The auth and hello services admit only internal traffic: a request counts only if it arrives over a same-project
VPC. A Cloud Run workload egresses to the public internet by default, so a caller with no VPC attachment reaches
the service's public front end and is rejected before IAM. This module provisions that VPC. The hello, sheaf and
evidence services attach to it because they call auth, and the sandbox Job because it calls hello. The web tier
attaches so that its calls to evidence and sheaf arrive over the VPC too. Each attaches with Direct VPC egress for
all traffic, so a call to a public run.app hostname traverses the VPC and is delivered internally, while the
subnet's Private Google Access keeps Google-API traffic (GCS, logging) on the private path.

This VPC is deliberately not sealed. The egress boundary is the sandbox guest's empty network namespace
(postern): the untrusted code reaches the outside world only through the hatch, never directly. The
trusted workloads on this network may reach the public internet: the sandbox worker
(sandbox-worker.md §"One trusted process, not two containers") and the web tier call Anthropic directly,
and the evidence service its public upstream hosts, so a Cloud NAT gives them a SNAT route out. Google
APIs and Cloud Run stay on the private path (subnet PGA); only non-Google public egress uses the NAT.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Distinct from the sandbox subnet (10.90/24) and the LB proxy-only subnet (10.91/24).
_SERVICES_SUBNET_CIDR = '10.92.0.0/24'


class ServicesNetwork(pulumi.ComponentResource):
    """The egress VPC and Private-Google-Access subnet of the web tier and the data-plane services.

    Attributes:
        network: The VPC they attach to (Direct VPC egress).
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
        # The SNAT route for public endpoints no private path covers (Anthropic, evidence's upstreams).
        router = gcp.compute.Router(
            'themis-services', project=project, region=region, network=self.network.id, opts=child
        )
        gcp.compute.RouterNat(
            'themis-services',
            project=project,
            region=region,
            router=router.name,
            nat_ip_allocate_option='AUTO_ONLY',
            source_subnetwork_ip_ranges_to_nat='ALL_SUBNETWORKS_ALL_IP_RANGES',
            opts=child,
        )
        self.register_outputs({'network': self.network.id, 'subnetwork': self.subnetwork.id})
