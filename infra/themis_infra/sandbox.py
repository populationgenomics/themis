"""Self-hosted sandbox: credentials, dispatcher, and the postern Cloud Run Job (sandbox-worker.md).

The self-hosted execution sandbox after the postern swap. It provisions:

- the use-only KMS MAC key that signs per-session tokens (§7) and the environment-key and
  webhook-signing-key secrets,
- the dispatcher service (HMAC-verified webhook → jobs.run), the load-balancer backend that admits only
  Anthropic's outbound range to it, and the sandbox Cloud Run Job.

The Job is a single trusted container: the EnvironmentWorker worker that runs each `run_python`
inside a postern bubblewrap sandbox (empty netns) whose only exit is a method-allowlisted hatch.
There is no dedicated VPC / egress firewall / NAT / DNS sinkhole and no internal load balancer —
the guest has zero network by construction, and the trusted worker reaches the internal-ingress
hello service over Direct VPC egress on the shared services network. The SA-scoped key/secret bindings are
granted where the consuming identities are wired (the program entrypoint).
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants

# One instance drains every webhook; the rest is headroom, since Anthropic disables an endpoint after
# ~20 consecutive failed deliveries.
_DISPATCHER_MAX_INSTANCES = 3

# Anthropic's published outbound IPv4 range (https://platform.claude.com/docs/en/api/ip-addresses).
ANTHROPIC_OUTBOUND_RANGE = '160.79.104.0/21'

# The dispatcher's webhook route, as `themis.services.dispatcher` serves it.
DISPATCHER_WEBHOOK_PATH = '/webhooks/anthropic'

# Cloud Armor's default rule: the lowest priority, matching every source. It cannot run in preview.
DEFAULT_RULE_PRIORITY = 2147483647

# The explicit deny-all above the default rule, so the deny can run in preview while the switch is off.
CATCH_ALL_PRIORITY = 2000


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
    per-session secret at rest. Who may sign with it is a `grants.SessionBearerDeriver`
    per holder.

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
    plaintext never enters the repo; replication is pinned in-region. The reader's grant
    is the dispatcher's (`grants.SecretReader`).

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
    """The dispatcher Cloud Run service, its narrowly-scoped SA, and its load-balancer backend.

    The service accepts traffic only from Cloud Load Balancing, and its backend service carries a Cloud
    Armor policy that admits `ANTHROPIC_OUTBOUND_RANGE` and denies every other source, so a request from
    outside Anthropic's range never reaches the container. The range is shared by every Anthropic
    customer, so the HMAC signature the dispatcher verifies stays the authentication. The program
    routes `DISPATCHER_WEBHOOK_PATH` on the web load balancer to `backend_service`.

    With `enforce_source_range` off, the deny rule runs in Cloud Armor preview mode: the load balancer
    log records each request it would deny, and every request reaches the service.

    Attributes:
        service_account_email: The runtime SA — env-key/webhook-key secret accessor, MAC signer, and
            (bound in the sandbox job) the job runner.
        backend_service: The load-balancer backend service fronting the dispatcher, with the Cloud
            Armor policy attached.
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
        enforce_source_range: bool,
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
            grants.SecretReader(
                'themis-dispatcher',
                member=member,
                secret=secret_id,
                project=project,
                target=label,
                prior=grants.Prior(f'themis-dispatcher-{label}-access', parent=self),
                opts=child,
            )
        grants.SessionBearerDeriver(
            'themis-dispatcher',
            member=member,
            key=session_token_key_id,
            prior=grants.Prior('themis-dispatcher-mac-signer', parent=self),
            opts=child,
        )
        # Version 1 is pinned: a different version derives different bearers and strands live sessions (§7).
        key_version = pulumi.Output.concat(session_token_key_id, '/cryptoKeyVersions/1')

        service = gcp.cloudrunv2.Service(
            'themis-dispatcher-service',
            project=project,
            name='themis-dispatcher',
            location=region,
            deletion_protection=False,
            # The run.app URL bypasses Cloud Armor, so only the load balancer (and same-project VPC traffic) may
            # reach the service.
            ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0, max_instance_count=_DISPATCHER_MAX_INSTANCES
                ),
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
        # Anthropic posts the webhook without a GCP token, so the IAM gate stays open; ingress and Cloud Armor
        # are the network gate, the HMAC signature the authentication.
        grants.PublicService(
            service=service.name,
            project=project,
            location=region,
            target='dispatcher',
            prior=grants.Prior('themis-dispatcher-public', parent=self),
            opts=child,
        )
        self.backend_service = _dispatcher_backend(
            project=project, region=region, service=service, enforce=enforce_source_range, child=child
        )
        self.register_outputs(
            {'service_account_email': self.service_account_email, 'backend_service': self.backend_service}
        )


def _dispatcher_backend(
    *,
    project: str,
    region: str,
    service: gcp.cloudrunv2.Service,
    enforce: bool,
    child: pulumi.ResourceOptions,
) -> pulumi.Output[str]:
    """Build the dispatcher's serverless NEG and its Cloud Armor-guarded backend service.

    Returns:
        The backend service's id, for the web load balancer's URL map.
    """
    policy = gcp.compute.SecurityPolicy(
        'themis-dispatcher-policy',
        project=project,
        description="Admits only Anthropic's published outbound range to the dispatcher webhook",
        type='CLOUD_ARMOR',
        rules=dispatcher_policy_rules(enforce=enforce),
        opts=child,
    )
    neg = gcp.compute.RegionNetworkEndpointGroup(
        'themis-dispatcher-neg',
        project=project,
        region=region,
        network_endpoint_type='SERVERLESS',
        cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service.name),
        opts=child,
    )
    backend = gcp.compute.BackendService(
        'themis-dispatcher-backend',
        project=project,
        protocol='HTTPS',
        load_balancing_scheme='EXTERNAL_MANAGED',
        backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
        security_policy=policy.self_link,
        # Each entry names the Cloud Armor rule that admitted or denied the request.
        log_config=gcp.compute.BackendServiceLogConfigArgs(enable=True, sample_rate=1.0),
        opts=child,
    )
    return backend.id


def dispatcher_policy_rules(*, enforce: bool) -> list[gcp.compute.SecurityPolicyRuleArgs]:
    """The dispatcher's Cloud Armor rules: admit Anthropic's outbound range, deny every other source.

    Cloud Armor refuses preview on the default rule, so the deny is an explicit catch-all above it.

    Args:
        enforce: Whether the deny takes effect. Off, the catch-all runs in preview, so the load-balancer
            log records each request it would deny, and the default rule lets the request through.
    """
    return [
        _source_rule(
            'allow',
            priority=1000,
            ranges=[ANTHROPIC_OUTBOUND_RANGE],
            description='Anthropic outbound range',
            preview=False,
        ),
        _source_rule(
            'deny(403)',
            priority=CATCH_ALL_PRIORITY,
            ranges=['*'],
            description='Deny every other source',
            preview=not enforce,
        ),
        _source_rule(
            'deny(403)' if enforce else 'allow',
            priority=DEFAULT_RULE_PRIORITY,
            ranges=['*'],
            description='Default rule',
            preview=False,
        ),
    ]


def _source_rule(
    action: str, *, priority: int, ranges: Sequence[str], description: str, preview: bool
) -> gcp.compute.SecurityPolicyRuleArgs:
    return gcp.compute.SecurityPolicyRuleArgs(
        action=action,
        priority=priority,
        description=description,
        preview=preview,
        match=gcp.compute.SecurityPolicyRuleMatchArgs(
            versioned_expr='SRC_IPS_V1',
            config=gcp.compute.SecurityPolicyRuleMatchConfigArgs(src_ip_ranges=list(ranges)),
        ),
    )


# The host uid and gid bwrap drops to, so the guest is a uid that owns nothing in the worker image rather than root
# (sandbox-worker.md §"The guest runs as a uid that owns nothing").
_GUEST_HOST_UID = 61000
_GUEST_HOST_GID = 61000


def _job_env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs:
    return gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(name=name, value=value)


class SandboxJob(pulumi.ComponentResource):
    """The sandbox Cloud Run Job (one trusted worker container) and its invoke-only SA.

    One execution per spawn: the worker runs ``EnvironmentWorker.handle_item()`` for the claimed session
    and its exit-0 completes the execution. The worker is trusted — it holds the environment key and the
    session token (injected per-execution by the dispatcher's ``jobs.run``, never baked) — and runs every
    ``shell`` command inside a postern bubblewrap sandbox, so no untrusted code shares the container.

    Direct VPC egress on the shared services network reaches the internal-ingress hello, evidence and sheaf
    services. There is no dedicated sandbox VPC and no
    egress firewall — the guest has zero network regardless, so containment rests on that rather than on
    shaping the trusted worker's egress.

    The Analysis repository the agent works in is a sheaf repository, mirrored inside the worker
    (sandbox-worker.md) and reached only through the sheaf service, which scopes every call by the session
    token the worker presents: the job holds no credential on the sheaf bucket and makes no auth call.

    Attributes:
        service_account_email: The job's runtime SA — ``run.invoker`` on the sheaf service its mirror runs
            over and on the hatch's forward targets, inert without the session token the worker holds; and a trace
            writer, for the spans of each session and its tool calls.
        job_name: The Job's name, for the dispatcher's ``run.jobs.run`` binding.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        worker_image: pulumi.Input[str],
        network: pulumi.Input[str],
        subnetwork: pulumi.Input[str],
        hello_url: pulumi.Input[str],
        evidence_url: pulumi.Input[str],
        sheaf_url: pulumi.Input[str],
        task_timeout_seconds: int,
        trace_sample_ratio: float,
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
        grants.TraceWriter(
            'themis-sandbox', member=grants.service_account(service_account.email), project=project, opts=child
        )

        job = gcp.cloudrunv2.Job(
            'themis-sandbox-job',
            project=project,
            location=region,
            name='themis-sandbox',
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=service_account.email,
                    # The ultimate backstop, sized to the longest legitimate session (§6); not Cloud Run's
                    # short default, which would kill a long agent-backgrounded computation.
                    timeout=f'{task_timeout_seconds}s',
                    max_retries=0,
                    # ALL_TRAFFIC so the internal-ingress hello service is reachable at its (public) run.app
                    # IP over the VPC — private-ranges-only would send that straight to the internet and the
                    # service would refuse it. Trade-off (sandbox-worker.md §"One trusted process, not two
                    # containers"): the trusted worker then has unrestricted public egress, so
                    # post-compromise exfil containment rests on the worker being trusted-only code
                    # and the guest having zero network.
                    vpc_access=gcp.cloudrunv2.JobTemplateTemplateVpcAccessArgs(
                        egress='ALL_TRAFFIC',
                        network_interfaces=[
                            gcp.cloudrunv2.JobTemplateTemplateVpcAccessNetworkInterfaceArgs(
                                network=network, subnetwork=subnetwork
                            )
                        ],
                    ),
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            name='worker',
                            image=worker_image,
                            envs=[
                                _job_env('THEMIS_HELLO_URL', hello_url),
                                _job_env('THEMIS_EVIDENCE_URL', evidence_url),
                                _job_env('THEMIS_SHEAF_URL', sheaf_url),
                                _job_env('THEMIS_SANDBOX_HOST_UID', str(_GUEST_HOST_UID)),
                                _job_env('THEMIS_SANDBOX_HOST_GID', str(_GUEST_HOST_GID)),
                                _job_env('THEMIS_TRACE_SAMPLE_RATIO', str(trace_sample_ratio)),
                            ],
                            # Bounds the trusted worker + the co-located guest together (the one-session
                            # blast radius review L3 accepts): a guest memory bomb OOMs only this execution.
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits={'cpu': '1', 'memory': '2Gi'}
                            ),
                        ),
                    ],
                ),
            ),
            opts=child,
        )
        self.job_name = job.name
        self.register_outputs({'service_account_email': self.service_account_email, 'job_name': self.job_name})
