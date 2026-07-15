"""Self-hosted sandbox: the network, credentials, dispatcher, and Cloud Run Job.

The self-hosted execution sandbox (docs/plans/self-hosted-sandbox.md §5-§8). It provisions:

- a VPC + regional subnet dedicated to the sandbox job's Direct VPC egress,
- a deny-all egress firewall opening only Anthropic's published API range — the internal store
  and hello services are reached through the internal load balancer (internal_lb.py), which opens
  its own egress rule to a dedicated IP,
- a Cloud DNS response policy that sinkholes every name except Anthropic (bypassed to its
  public host) — the load balancer adds the internal-service records (§8),
- the use-only KMS MAC key that signs per-session tokens (§7) and the environment-key and
  webhook-signing-key secrets,
- the dispatcher service and the sandbox Cloud Run Job (agent + credential proxy).

The network is dedicated to the sandbox job — the sole Direct-VPC-egress tenant — so the
egress rules govern it network-wide without a per-SA target. The SA-scoped key/secret
bindings are granted where the consuming identities are wired (the program entrypoint).
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

# The agent and proxy share this in-memory workspace (restore/checkpoint, §6, §9); the harness
# output dir is a required mount whose content is discarded (deliverables go to the document).
_WORKSPACE_MOUNT = '/workspace'
_SESSION_OUTPUTS_MOUNT = '/mnt/session/outputs'


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


def webhook_signing_key_secret(
    *,
    project: str,
    region: str,
    signing_key: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.secretmanager.Secret:
    """Provision the Anthropic webhook signing key (``whsec_…``) as a Secret Manager secret (§5).

    The key the dispatcher's webhook endpoint verifies deliveries against (Standard Webhooks). Console-
    generated per environment, sourced from encrypted config; read only by the dispatcher SA.
    """
    secret = gcp.secretmanager.Secret(
        'themis-anthropic-webhook-signing-key',
        project=project,
        secret_id='anthropic-webhook-signing-key',  # noqa: S106 — the secret's name, not its value
        replication=gcp.secretmanager.SecretReplicationArgs(
            user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                replicas=[gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(location=region)],
            ),
        ),
        opts=opts,
    )
    gcp.secretmanager.SecretVersion(
        'themis-anthropic-webhook-signing-key-current',
        secret=secret.id,
        secret_data=signing_key,
        opts=pulumi.ResourceOptions(parent=secret),
    )
    return secret


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


def _secret_env(name: str, secret_id: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name=name,
        value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
            secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                secret=secret_id, version='latest'
            ),
        ),
    )


class DispatcherService(pulumi.ComponentResource):
    """The dispatcher Cloud Run service (public HMAC webhook) and its narrowly-scoped SA.

    Attributes:
        service_account_email: The runtime SA — env-key/webhook-key secret accessor, MAC signer, and
            (bound in the sandbox job) the job runner.
        url: The public service URL Anthropic's webhook posts to.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        environment_id: str,
        environment_key_secret_id: pulumi.Input[str],
        webhook_signing_key_secret_id: pulumi.Input[str],
        session_token_key_id: pulumi.Input[str],
        sandbox_job_name: pulumi.Input[str],
        reclaim_older_than_ms: int,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:DispatcherService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-dispatcher-runtime',
            project=project,
            account_id='themis-dispatcher',
            display_name='Themis sandbox dispatcher runtime',
            opts=child,
        )
        self.service_account_email = service_account.email
        member = pulumi.Output.concat('serviceAccount:', service_account.email)

        for label, secret_id in (
            ('environment-key', environment_key_secret_id),
            ('webhook-signing-key', webhook_signing_key_secret_id),
        ):
            gcp.secretmanager.SecretIamMember(
                f'themis-dispatcher-{label}-access',
                project=project,
                secret_id=secret_id,
                role='roles/secretmanager.secretAccessor',
                member=member,
                opts=child,
            )
        # Use-only MAC signing (KMS material never leaves KMS); the dispatcher derives HMAC(session_id).
        gcp.kms.CryptoKeyIAMMember(
            'themis-dispatcher-mac-signer',
            crypto_key_id=session_token_key_id,
            role='roles/cloudkms.signerVerifier',
            member=member,
            opts=child,
        )
        # Version 1 is pinned: a different version derives different bearers and strands live sessions (§7).
        key_version = pulumi.Output.concat(session_token_key_id, '/cryptoKeyVersions/1')

        service = gcp.cloudrunv2.Service(
            'themis-dispatcher-service',
            project=project,
            name='themis-dispatcher',
            location=region,
            ingress='INGRESS_TRAFFIC_ALL',  # public: Anthropic cannot present a GCP token; HMAC is the auth (§5)
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            _env('ANTHROPIC_ENVIRONMENT_ID', environment_id),
                            _env('THEMIS_SESSION_TOKEN_KEY_VERSION', key_version),
                            _env('THEMIS_SANDBOX_JOB_PROJECT', project),
                            _env('THEMIS_SANDBOX_JOB_REGION', region),
                            _env('THEMIS_SANDBOX_JOB_NAME', sandbox_job_name),
                            _env('THEMIS_RECLAIM_OLDER_THAN_MS', str(reclaim_older_than_ms)),
                            _secret_env('ANTHROPIC_ENVIRONMENT_KEY', environment_key_secret_id),
                            _secret_env('ANTHROPIC_WEBHOOK_SIGNING_KEY', webhook_signing_key_secret_id),
                        ],
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(container_port=8080),  # HTTP/1.1
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            http_get=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeHttpGetArgs(path='/healthz'),
                        ),
                    )
                ],
            ),
            opts=child,
        )
        self.url = service.uri
        # Anthropic posts the webhook without a GCP token, so the endpoint is public-invoke; the HMAC
        # signature is the authentication and the proxy allowlist keeps the sandbox off /work/stats (§11).
        gcp.cloudrunv2.ServiceIamMember(
            'themis-dispatcher-public',
            project=project,
            location=region,
            name=service.name,
            role='roles/run.invoker',
            member='allUsers',
            opts=child,
        )
        self.register_outputs({'service_account_email': self.service_account_email, 'url': self.url})


def _job_env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs:
    return gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(name=name, value=value)


class SandboxJob(pulumi.ComponentResource):
    """The sandbox Cloud Run Job (agent + proxy sidecar) and its invoke-only SA.

    One execution per spawn: the agent (main) runs ``ant beta:worker run`` and its exit-0 completes the
    execution; the proxy sidecar holds the credentials. The agent depends on the proxy's startup probe,
    which the proxy passes only after it restores /workspace — so the agent always boots restored.
    Per-execution secrets (env key, session token, ids) are injected by the dispatcher's ``jobs.run``,
    never baked here.

    Attributes:
        service_account_email: The job's runtime SA — ``run.invoker`` on the sandbox-reachable services
            only; inert without the session token the proxy holds.
        job_name: The Job's name, for the dispatcher's ``run.jobs.run`` binding.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        agent_image: pulumi.Input[str],
        proxy_image: pulumi.Input[str],
        network: pulumi.Input[str],
        subnetwork: pulumi.Input[str],
        store_url: pulumi.Input[str],
        hello_url: pulumi.Input[str],
        internal_ca_cert: pulumi.Input[str],
        forward_methods: str,
        spki_pins: pulumi.Input[str],
        working_document_path: str,
        task_timeout_seconds: int,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:SandboxJob', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-sandbox-job',
            project=project,
            account_id='themis-sandbox-job',
            display_name='Themis sandbox job runtime',
            opts=child,
        )
        self.service_account_email = service_account.email

        job = gcp.cloudrunv2.Job(
            'themis-sandbox-job',
            project=project,
            location=region,
            name='themis-sandbox',
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=service_account.email,
                    # The ultimate backstop, sized to the longest legitimate session (§6); not Cloud Run's
                    # short default, which would kill a long agent-backgrounded computation.
                    timeout=f'{task_timeout_seconds}s',
                    max_retries=0,
                    vpc_access=gcp.cloudrunv2.JobTemplateTemplateVpcAccessArgs(
                        egress='ALL_TRAFFIC',
                        network_interfaces=[
                            gcp.cloudrunv2.JobTemplateTemplateVpcAccessNetworkInterfaceArgs(
                                network=network, subnetwork=subnetwork
                            )
                        ],
                    ),
                    containers=[
                        # agent first = main: its exit-0 completes the execution while the sidecar is torn
                        # down (§12); it depends on the proxy's post-restore readiness.
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            name='agent',
                            image=agent_image,
                            # `ant beta:worker run` stops this long after a turn's end_turn (default 1m); widened
                            # so the proxy's post-idle checkpoint has room to ride out a cold-started store
                            # within the window (§9). The sandbox is billed only for this idle, per session.
                            args=['--max-idle', '300s'],
                            depends_ons=['proxy'],
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits={'cpu': '1', 'memory': '2Gi'}
                            ),
                            volume_mounts=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerVolumeMountArgs(
                                    name='workspace', mount_path=_WORKSPACE_MOUNT
                                ),
                                gcp.cloudrunv2.JobTemplateTemplateContainerVolumeMountArgs(
                                    name='session-outputs', mount_path=_SESSION_OUTPUTS_MOUNT
                                ),
                            ],
                        ),
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            name='proxy',
                            image=proxy_image,
                            envs=[
                                _job_env('THEMIS_STORE_URL', store_url),
                                _job_env('THEMIS_FORWARD_UPSTREAM_URL', hello_url),
                                _job_env('THEMIS_INTERNAL_CA_CERT', internal_ca_cert),
                                _job_env('THEMIS_FORWARD_METHODS', forward_methods),
                                _job_env('THEMIS_ANTHROPIC_SPKI_PINS', spki_pins),
                                _job_env('THEMIS_WORKING_DOCUMENT_PATH', working_document_path),
                                # The DNS sinkhole answers metadata.google.internal with 0.0.0.0; reach the
                                # metadata server by IP so google-auth mints the store ID token.
                                _job_env('GCE_METADATA_HOST', '169.254.169.254'),
                            ],
                            # Buffers the whole workspace archive in memory to checkpoint (a copy of the
                            # scratch tmpfs, below) on top of the runtime.
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits={'cpu': '1', 'memory': '2Gi'}
                            ),
                            # Binds only after restore, so the agent's dependency means "restore complete".
                            # The window must cover worst-case restore — cold-starting the store and auth
                            # services plus the workspace fetch — so the default 30s (3 x 10s) is too tight.
                            startup_probe=gcp.cloudrunv2.JobTemplateTemplateContainerStartupProbeArgs(
                                tcp_socket=gcp.cloudrunv2.JobTemplateTemplateContainerStartupProbeTcpSocketArgs(
                                    port=8080
                                ),
                                period_seconds=10,
                                failure_threshold=18,
                            ),
                            volume_mounts=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerVolumeMountArgs(
                                    name='workspace', mount_path=_WORKSPACE_MOUNT
                                ),
                            ],
                        ),
                    ],
                    volumes=[
                        gcp.cloudrunv2.JobTemplateTemplateVolumeArgs(
                            name='workspace',
                            # Below the store's archive cap (servicer.py) with margin: the checkpoint tars
                            # this scratch whole, and the tar of a full scratch must stay under that cap.
                            empty_dir=gcp.cloudrunv2.JobTemplateTemplateVolumeEmptyDirArgs(
                                medium='MEMORY', size_limit='384Mi'
                            ),
                        ),
                        gcp.cloudrunv2.JobTemplateTemplateVolumeArgs(
                            name='session-outputs',
                            empty_dir=gcp.cloudrunv2.JobTemplateTemplateVolumeEmptyDirArgs(
                                medium='MEMORY', size_limit='128Mi'
                            ),
                        ),
                    ],
                ),
            ),
            opts=child,
        )
        self.job_name = job.name
        self.register_outputs({'service_account_email': self.service_account_email, 'job_name': self.job_name})
