"""Regional internal Application Load Balancer fronting the sandbox's internal gRPC services.

The sandbox job runs in a locked-down VPC (deny-all egress, DNS sinkhole) yet must reach the
internal-ingress store and hello services (docs/plans/self-hosted-sandbox.md §8). A regional internal
Application Load Balancer gives them stable RFC1918 addresses behind private hostnames, so sandbox
egress narrows to that one IP. The rejected alternative — Google's restricted VIP — is a single
address shared by every ``*.run.app`` service and cannot be filtered by host, so it can't contain
workspace exfiltration; a dedicated internal IP can.

gRPC forces an HTTPS frontend: a GCP Application Load Balancer only negotiates HTTP/2 with clients
over TLS (there is no cleartext-HTTP/2 frontend), and the private hostname rules out a Google-managed
certificate (no public domain to DNS-authorize). So the LB presents a self-signed certificate whose
PEM the sandbox proxy trusts as its gRPC root (``ca_cert_pem``). One forwarding rule host-routes all
fronted services, so they share a single IP.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_gcp as gcp
import pulumi_tls as tls

# The Envoy proxies the regional ALB runs draw from this subnet; it must not overlap the sandbox
# subnet (sandbox.py). One such subnet per region per network serves every regional ALB.
_PROXY_ONLY_SUBNET_CIDR = '10.91.0.0/24'

# The sandbox reaches each fronted service at a private hostname resolved (by the sandbox DNS
# response policy) to the shared LB IP. Shared with the program entrypoint, which sets each
# service's custom audience and job env to the matching ``https://<host>``.
STORE_HOST = 'store.internal.themis'
HELLO_HOST = 'hello.internal.themis'

# Self-signed, so we own rotation; issued long-lived to keep that a non-event.
_CERT_VALIDITY_HOURS = 10 * 365 * 24


def audience(host: str) -> str:
    """The ID-token audience (and dialed URL) for a fronted host: ``https://<host>``."""
    return f'https://{host}'


def _slug(host: str) -> str:
    return host.replace('.', '-')


class InternalServiceLoadBalancer(pulumi.ComponentResource):
    """A regional internal ALB host-routing gRPC traffic to internal Cloud Run services.

    Fronts each ``host -> Cloud Run service`` on one internal IP, adds the private-hostname DNS
    records to the sandbox response policy, and opens sandbox egress to the LB. The proxy trusts
    ``ca_cert_pem`` as the gRPC root for every fronted host.

    Attributes:
        ip_address: The shared internal VIP the fronted hostnames resolve to.
        ca_cert_pem: The self-signed serving certificate's PEM — the sandbox proxy's gRPC trust root.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        network: pulumi.Input[str],
        subnetwork: pulumi.Input[str],
        response_policy: pulumi.Input[str],
        services: Mapping[str, pulumi.Input[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:InternalServiceLoadBalancer', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)
        hosts = sorted(services)

        proxy_subnet = gcp.compute.Subnetwork(
            'themis-sandbox-proxy-only',
            project=project,
            name='themis-sandbox-proxy-only',
            region=region,
            network=network,
            ip_cidr_range=_PROXY_ONLY_SUBNET_CIDR,
            # The managed-proxy subnet the regional ALB's Envoys run in — no instances draw from it.
            purpose='REGIONAL_MANAGED_PROXY',
            role='ACTIVE',
            opts=child,
        )
        address = gcp.compute.Address(
            'themis-internal-lb',
            project=project,
            name='themis-internal-lb',
            region=region,
            address_type='INTERNAL',
            subnetwork=subnetwork,
            opts=child,
        )
        self.ip_address = address.address

        private_key = tls.PrivateKey('themis-internal-lb', algorithm='RSA', rsa_bits=2048, opts=child)
        certificate = tls.SelfSignedCert(
            'themis-internal-lb',
            private_key_pem=private_key.private_key_pem,
            subject=tls.SelfSignedCertSubjectArgs(common_name='themis-internal-services'),
            dns_names=hosts,
            validity_period_hours=_CERT_VALIDITY_HOURS,
            is_ca_certificate=True,  # the sandbox proxy trusts this one cert directly as its gRPC root
            allowed_uses=['key_encipherment', 'digital_signature', 'server_auth'],
            opts=child,
        )
        self.ca_cert_pem = certificate.cert_pem
        region_certificate = gcp.compute.RegionSslCertificate(
            'themis-internal-lb',
            project=project,
            region=region,
            certificate=certificate.cert_pem,
            private_key=private_key.private_key_pem,
            opts=child,
        )

        backends = {host: self._backend('themis', project, region, host, services[host], child) for host in hosts}
        url_map = gcp.compute.RegionUrlMap(
            'themis-internal-lb',
            project=project,
            region=region,
            # Unmatched hosts fall through to a fronted backend, which rejects the unknown gRPC method.
            default_service=backends[hosts[0]].id,
            host_rules=[gcp.compute.RegionUrlMapHostRuleArgs(hosts=[host], path_matcher=_slug(host)) for host in hosts],
            path_matchers=[
                gcp.compute.RegionUrlMapPathMatcherArgs(name=_slug(host), default_service=backends[host].id)
                for host in hosts
            ],
            opts=child,
        )
        target_proxy = gcp.compute.RegionTargetHttpsProxy(
            'themis-internal-lb',
            project=project,
            region=region,
            url_map=url_map.id,
            ssl_certificates=[region_certificate.id],
            opts=child,
        )
        gcp.compute.ForwardingRule(
            'themis-internal-lb',
            project=project,
            region=region,
            load_balancing_scheme='INTERNAL_MANAGED',
            target=target_proxy.id,
            ip_address=address.id,
            port_range='443',
            network=network,
            subnetwork=subnetwork,
            # The proxy-only subnet must exist in the region before the rule; it is not referenced directly.
            opts=pulumi.ResourceOptions(parent=self, depends_on=[proxy_subnet]),
        )

        for host in hosts:
            gcp.dns.ResponsePolicyRule(
                f'themis-internal-{_slug(host)}',
                project=project,
                response_policy=response_policy,
                rule_name=f'internal-{_slug(host)}',
                dns_name=f'{host}.',
                local_data=gcp.dns.ResponsePolicyRuleLocalDataArgs(
                    local_datas=[
                        # The local-data name must equal the rule's dns_name.
                        gcp.dns.ResponsePolicyRuleLocalDataLocalDataArgs(
                            name=f'{host}.', type='A', ttl=300, rrdatas=[address.address]
                        )
                    ],
                ),
                opts=child,
            )
        gcp.compute.Firewall(
            'themis-sandbox-egress-internal-lb',
            project=project,
            network=network,
            direction='EGRESS',
            priority=1000,
            allows=[gcp.compute.FirewallAllowArgs(protocol='tcp', ports=['443'])],
            destination_ranges=[address.address.apply(lambda ip: f'{ip}/32')],
            opts=child,
        )

        self.register_outputs({'ip_address': self.ip_address, 'ca_cert_pem': self.ca_cert_pem})

    def _backend(
        self,
        name: str,
        project: str,
        region: str,
        host: str,
        service_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions,
    ) -> gcp.compute.RegionBackendService:
        neg = gcp.compute.RegionNetworkEndpointGroup(
            f'{name}-{_slug(host)}-neg',
            project=project,
            region=region,
            network_endpoint_type='SERVERLESS',
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service_name),
            opts=opts,
        )
        return gcp.compute.RegionBackendService(
            f'{name}-{_slug(host)}-backend',
            project=project,
            region=region,
            load_balancing_scheme='INTERNAL_MANAGED',
            # Ignored for a serverless NEG: Cloud Run negotiates HTTP/2 to the container itself.
            protocol='HTTP',
            backends=[gcp.compute.RegionBackendServiceBackendArgs(group=neg.id)],
            opts=opts,
        )
