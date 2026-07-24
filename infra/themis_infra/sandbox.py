"""Self-hosted sandbox: credentials, dispatcher, and the postern Cloud Run Job (postern-sandbox-swap.md).

The self-hosted execution sandbox after the postern swap. It provisions:

- the use-only KMS MAC key that signs per-session tokens (§7) and the environment-key and
  webhook-signing-key secrets,
- the dispatcher service (public HMAC webhook → jobs.run) and the sandbox Cloud Run Job.

The Job is a single trusted container: the EnvironmentWorker worker that runs each `run_python`
inside a postern bubblewrap sandbox (empty netns) whose only exit is a method-allowlisted hatch.
There is no dedicated VPC / egress firewall / NAT / DNS sinkhole and no internal load balancer —
the guest has zero network by construction, and the trusted worker reaches the internal-ingress
store over Direct VPC egress on the shared services network. The SA-scoped key/secret bindings are
granted where the consuming identities are wired (the program entrypoint).
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


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
            deletion_protection=False,
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
    """The sandbox Cloud Run Job (one trusted worker container) and its invoke-only SA.

    One execution per spawn: the worker runs ``EnvironmentWorker.handle_item()`` for the claimed session
    and its exit-0 completes the execution. The worker is trusted — it holds the environment key and the
    session token (injected per-execution by the dispatcher's ``jobs.run``, never baked) — and runs every
    ``shell`` command inside a postern bubblewrap sandbox, so no untrusted code shares the container.

    Direct VPC egress on the shared services network reaches the internal-ingress store and hello services;
    egress is private-ranges-only, so those go over the VPC while Anthropic (public) uses Cloud Run's
    managed egress — no NAT, no egress firewall (the guest has zero network regardless).

    Attributes:
        service_account_email: The job's runtime SA — ``run.invoker`` on the store and hello services (the
            hatch's forward targets); inert without the session token the worker holds.
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
        store_url: pulumi.Input[str],
        hello_url: pulumi.Input[str],
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
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=service_account.email,
                    # The ultimate backstop, sized to the longest legitimate session (§6); not Cloud Run's
                    # short default, which would kill a long agent-backgrounded computation.
                    timeout=f'{task_timeout_seconds}s',
                    max_retries=0,
                    # ALL_TRAFFIC so the internal-ingress store is reachable at its (public) run.app IP over
                    # the VPC — private-ranges-only would send that straight to the internet and the store
                    # would refuse it. Trade-off (postern-sandbox-swap.md §2): the trusted worker then has
                    # unrestricted public egress, so post-compromise exfil containment rests on the worker
                    # being trusted-only code and the guest having zero network.
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
                                _job_env('THEMIS_STORE_URL', store_url),
                                _job_env('THEMIS_HELLO_URL', hello_url),
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
