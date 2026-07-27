"""The litcache Dataflow ingestion workers' dedicated network.

The seed-ingestion pipeline (`litcache/ingest_beam.py`) runs on Dataflow. Its
workers need nothing from the internal service mesh — they read GCS, call
NCBI/OpenAlex, and mint into Cloud SQL over the connector, all public or Google
endpoints. So they run here, off the internal services VPC, which exists to gate
the internal-ingress auth service and has no business carrying a batch job.

Workers take external IPs, so all egress — Google and non-Google alike — leaves
through them and no Cloud NAT is needed. External IPs over a NAT is deliberate:
it avoids the gateway cost, at the price of publicly-routable workers and no
single audited egress path. Inbound stays closed regardless (implied deny plus
one intra-subnet allow). Private Google Access is set as insurance — inert while
the workers have external IPs, it keeps GCS on the private path if
`--no_use_public_ips` is ever adopted (which would then also need a Cloud NAT, or
the non-Google endpoints black-hole).

A Pulumi-created network does not inherit the auto-project `default-allow-internal`
rule, so the Runner v2 worker-to-worker firewall is declared explicitly.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Distinct from the services subnet (10.92/24).
_INGEST_SUBNET_CIDR = '10.93.0.0/24'


class IngestionNetwork(pulumi.ComponentResource):
    """The Dataflow ingestion workers' VPC, subnet, and worker-comms firewall.

    Attributes:
        network: The VPC the Dataflow workers attach to.
        subnetwork: The regional subnet the workers draw addresses from.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:IngestionNetwork', 'themis-ingest', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.network = gcp.compute.Network(
            'themis-ingest',
            project=project,
            name='themis-ingest',
            auto_create_subnetworks=False,
            opts=child,
        )
        self.subnetwork = gcp.compute.Subnetwork(
            'themis-ingest',
            project=project,
            name='themis-ingest',
            region=region,
            network=self.network.id,
            ip_cidr_range=_INGEST_SUBNET_CIDR,
            # Insurance: inert while workers have external IPs; carries GCS on the private path only if
            # public IPs are ever dropped.
            private_ip_google_access=True,
            opts=child,
        )
        # A Pulumi-created network has no default-allow-internal rule; Runner v2 workers reach each
        # other on the harness ports only through this explicit ingress allow.
        gcp.compute.Firewall(
            'themis-ingest-workers',
            project=project,
            network=self.network.id,
            direction='INGRESS',
            source_ranges=[self.subnetwork.ip_cidr_range],
            allows=[gcp.compute.FirewallAllowArgs(protocol='tcp', ports=['12345-12346'])],
            opts=child,
        )
        self.register_outputs({'network': self.network.id, 'subnetwork': self.subnetwork.id})
