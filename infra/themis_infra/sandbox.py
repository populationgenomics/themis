"""Self-hosted sandbox substrate: the network, DNS, and key material the sandbox job builds on.

The network + credential foundation for the self-hosted execution sandbox
(docs/plans/self-hosted-sandbox.md §7, §8), split out ahead of the dispatcher and
sandbox-job compute (which land on this same module in a later slice). It provisions:

- a VPC + regional subnet dedicated to the sandbox job's Direct VPC egress,
- a deny-all egress firewall that opens only Anthropic's published API range,
- a Cloud DNS response policy that sinkholes every name except the exact hosts the
  sandbox needs — closing the DNS exfiltration channel (§8),
- the use-only KMS MAC key that signs per-session tokens (§7),
- the Anthropic environment-key secret (its runtime reader, the dispatcher, lands
  with the compute slice).

The network is dedicated to the sandbox job — the sole Direct-VPC-egress tenant — so
the egress rules govern it network-wide without the job's SA (which does not exist
yet). The internal-store egress route, the store-host DNS bypass, and the SA-scoped
key/secret bindings all attach with the compute slice, where the store URL and the
consuming identities exist.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Anthropic's published, dedicated inbound API range — "will not change without notice"
# (https://platform.claude.com/docs/en/api/ip-addresses). Pinned directly so the egress
# rule needs no firewall-side FQDN resolution, and the block is Anthropic-owned so an
# SNI-mismatched connection reaches no co-tenant (§8). IPv6 (2607:6bc0::/48) is additive
# once the subnet is dual-stack.
_ANTHROPIC_API_CIDR = '160.79.104.0/23'
_ANTHROPIC_API_HOST = 'api.anthropic.com'

_SANDBOX_SUBNET_CIDR = '10.90.0.0/24'


class SandboxNetwork(pulumi.ComponentResource):
    """The sandbox job's dedicated VPC, egress firewall, and DNS response policy.

    Attributes:
        network: The VPC the sandbox job attaches to (Direct VPC egress).
        subnetwork: The regional subnet the job's instances draw addresses from.
        response_policy: The Cloud DNS response policy on the network; the compute
            slice adds the store-host bypass rule to it.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:SandboxNetwork', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.network = gcp.compute.Network(
            'themis-sandbox',
            project=project,
            name='themis-sandbox',
            auto_create_subnetworks=False,
            opts=child,
        )
        self.subnetwork = gcp.compute.Subnetwork(
            'themis-sandbox',
            project=project,
            name='themis-sandbox',
            region=region,
            network=self.network.id,
            ip_cidr_range=_SANDBOX_SUBNET_CIDR,
            # Reach Google APIs (and internal Cloud Run) over the private path, never
            # a public IP; the job's instances carry no external address.
            private_ip_google_access=True,
            opts=child,
        )

        # Egress lockdown (§8): deny all, then admit only Anthropic's API range on :443.
        # The subnet is sandbox-dedicated, so these govern the job network-wide without a
        # target SA/tag; the internal-store route lands with the SA that reaches it.
        gcp.compute.Firewall(
            'themis-sandbox-egress-deny-all',
            project=project,
            network=self.network.id,
            direction='EGRESS',
            priority=65534,
            denies=[gcp.compute.FirewallDenyArgs(protocol='all')],
            destination_ranges=['0.0.0.0/0'],
            opts=child,
        )
        gcp.compute.Firewall(
            'themis-sandbox-egress-anthropic',
            project=project,
            network=self.network.id,
            direction='EGRESS',
            priority=1000,
            allows=[gcp.compute.FirewallAllowArgs(protocol='tcp', ports=['443'])],
            destination_ranges=[_ANTHROPIC_API_CIDR],
            opts=child,
        )
        # The job's instances carry no external address, so reaching Anthropic — the one public
        # destination the egress firewall admits — needs a SNAT path. Cloud NAT provides it; the
        # firewall above (Anthropic CIDR only) stays the destination seal, NAT just the route. The
        # internal load balancer is an RFC1918 address, reached over the VPC without NAT.
        router = gcp.compute.Router(
            'themis-sandbox', project=project, region=region, network=self.network.id, opts=child
        )
        gcp.compute.RouterNat(
            'themis-sandbox',
            project=project,
            region=region,
            router=router.name,
            nat_ip_allocate_option='AUTO_ONLY',
            source_subnetwork_ip_ranges_to_nat='LIST_OF_SUBNETWORKS',
            subnetworks=[
                gcp.compute.RouterNatSubnetworkArgs(name=self.subnetwork.id, source_ip_ranges_to_nats=['ALL_IP_RANGES'])
            ],
            opts=child,
        )
        # DNS lockdown (§8): sinkhole every name locally so a query for an
        # attacker-controlled name never recurses to an external nameserver (the query
        # reaching it is the leak), with exact-name bypasses for the hosts the sandbox
        # needs. The metadata resolver (169.254.169.254) forwards to this policy, so it
        # is governed too, not a bypass.
        self.response_policy = gcp.dns.ResponsePolicy(
            'themis-sandbox',
            project=project,
            response_policy_name='themis-sandbox',
            networks=[gcp.dns.ResponsePolicyNetworkArgs(network_url=self.network.id)],
            opts=child,
        )
        gcp.dns.ResponsePolicyRule(
            'themis-sandbox-sinkhole',
            project=project,
            response_policy=self.response_policy.response_policy_name,
            rule_name='sinkhole-all',
            dns_name='*.',
            # Answered locally with a non-routable address: resolved without recursion,
            # and the egress firewall drops any connection to it regardless.
            local_data=gcp.dns.ResponsePolicyRuleLocalDataArgs(
                local_datas=[
                    gcp.dns.ResponsePolicyRuleLocalDataLocalDataArgs(
                        name='*.',
                        type='A',
                        ttl=300,
                        rrdatas=['0.0.0.0'],  # noqa: S104 — DNS sinkhole target, not a socket bind
                    )
                ],
            ),
            opts=child,
        )
        gcp.dns.ResponsePolicyRule(
            'themis-sandbox-bypass-anthropic',
            project=project,
            response_policy=self.response_policy.response_policy_name,
            rule_name='bypass-anthropic',
            # Exact, not `*.anthropic.com` — a subdomain under an allowed parent must not
            # tunnel out (§8).
            dns_name=f'{_ANTHROPIC_API_HOST}.',
            behavior='bypassResponsePolicy',
            opts=child,
        )

        self.register_outputs(
            {
                'network': self.network.id,
                'subnetwork': self.subnetwork.id,
                'response_policy': self.response_policy.response_policy_name,
            }
        )


def session_token_signing_key(
    *,
    project: str,
    region: str,
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.kms.CryptoKey:
    """Provision the use-only KMS MAC key that signs per-session tokens (§7).

    An HMAC-SHA256 MAC key in the environment's existing `themis` key ring. The key
    material never leaves KMS — the BFF and dispatcher derive `HMAC(session_id)` via the
    MAC-sign API — so no service can exfiltrate the signing key, and there is no
    per-session secret at rest. The `signerVerifier` grants to the BFF and dispatcher SAs
    land with those services.

    Args:
        project: The GCP project the key ring lives in.
        region: The key ring's location.
        opts: Resource options (parent/dependency wiring).

    Returns:
        The MAC `CryptoKey`; callers sign against its primary version.
    """
    key_ring = gcp.kms.get_kms_key_ring(name='themis', location=region, project=project)
    return gcp.kms.CryptoKey(
        'themis-session-token-signing-key',
        key_ring=key_ring.id,
        purpose='MAC',
        version_template=gcp.kms.CryptoKeyVersionTemplateArgs(algorithm='HMAC_SHA256'),
        opts=opts,
    )


def environment_key_secret(
    *,
    project: str,
    region: str,
    environment_key: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.secretmanager.Secret:
    """Provision the Anthropic environment key as a Secret Manager secret (§7).

    The worker credential (`sk-ant-oat01-…`) that authorises claiming work and posting
    results for the whole environment. Sourced from encrypted Pulumi config so the
    plaintext never enters the repo; replication is pinned in-region. No accessor grant
    is attached here — the runtime reader (the dispatcher) is not deployed yet; its
    `secretAccessor` grant lands with that service.

    Args:
        project: The GCP project to create the secret in.
        region: The single region the secret replicates to.
        environment_key: The key value, from `config.require_secret` (kept secret).
        opts: Resource options (parent/dependency wiring).

    Returns:
        The `Secret`; its latest version carries `environment_key`.
    """
    secret = gcp.secretmanager.Secret(
        'themis-anthropic-environment-key',
        project=project,
        secret_id='anthropic-environment-key',  # noqa: S106 — the secret's name, not its value
        replication=gcp.secretmanager.SecretReplicationArgs(
            user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                replicas=[gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(location=region)],
            ),
        ),
        opts=opts,
    )
    gcp.secretmanager.SecretVersion(
        'themis-anthropic-environment-key-current',
        secret=secret.id,
        secret_data=environment_key,
        opts=pulumi.ResourceOptions(parent=secret),
    )
    return secret
