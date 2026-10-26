"""The full-text work lane: two Cloud Tasks queues and the one worker both deliver to.

Architecture B (`docs/design/evidence-fulltext.md`): all fetch/convert work lives in a separate worker,
so the evidence read image stays lean. A Cloud Task pushes `POST /convert` with a `WorkRequest` to the worker,
which runs the litcache producer (OA XML → markdown, else PDF LLM-OCR) off any request path.

The evidence service puts its requests on the interactive queue: `MaybeIngestPapers` creates one task
per paper that comes back PENDING, named for the paper and the main-text target, whether the paper was
already in the corpus or brought in by that same call. Every other writer, and the sweep, uses the bulk
queue, so an agent's request never waits behind a bulk run's backlog. The grants to enqueue are the
program's, since neither a queue nor the invoker knows its caller.

- `WorkQueues` — the interactive and bulk Cloud Tasks queues. Their concurrency caps are capacity
  limits: they protect the worker's memory and pace its calls out, and do not bound spend; bounded
  retries stop a request that keeps failing.
- `ConvertWorker` — the worker Cloud Run service (IAM-gated, require-auth) and its runtime SA
  (object read/write on the litcache bucket; the Claude API by workload identity federation; each
  transcription's token counts to Cloud Monitoring through the Telemetry API).
- `ConversionInvoker` — the identity whose OIDC token the task carries: `run.invoker` on the worker,
  and the Cloud Tasks service agent may mint its token. Whatever enqueues needs `actAs` on it.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants

# How many requests each queue dispatches at once. The worker scales to their sum, so a full bulk queue
# never takes an instance an interactive request needs. Tune against observed volume.
_INTERACTIVE_CONCURRENT_DISPATCHES = 5
_BULK_CONCURRENT_DISPATCHES = 5
# Bounded retries. Cloud Tasks has no dead-letter destination — an exhausted task is simply deleted,
# leaving the paper as it was for the sweep to find. 1 attempt + 4 retries at a doubling 30s spreads a
# transient failure (model-API overload, a fetch blip) over ~7.5 minutes; the ceiling binds only if the
# attempt count is raised. The job log, not this count, bounds what one PDF's transcription costs.
_MAX_ATTEMPTS = 5
_MIN_BACKOFF = '30s'
_MAX_BACKOFF = '600s'
# The request timeout: Cloud Tasks' 30-minute dispatch-deadline maximum, which the enqueuer sets on
# each task (`themis.litcache.work.DISPATCH_DEADLINE`). Equal, so neither side abandons a request the
# other is still running, and the job log reads a begun attempt older than this as dead.
_REQUEST_TIMEOUT = '1800s'


def _queue(
    name: str, *, project: str, region: str, concurrent_dispatches: int, opts: pulumi.ResourceOptions | None
) -> gcp.cloudtasks.Queue:
    return gcp.cloudtasks.Queue(
        name,
        project=project,
        location=region,
        name=name,
        rate_limits=gcp.cloudtasks.QueueRateLimitsArgs(max_concurrent_dispatches=concurrent_dispatches),
        retry_config=gcp.cloudtasks.QueueRetryConfigArgs(
            max_attempts=_MAX_ATTEMPTS,
            min_backoff=_MIN_BACKOFF,
            max_backoff=_MAX_BACKOFF,
        ),
        opts=opts,
    )


class WorkQueues(pulumi.ComponentResource):
    """The interactive and bulk queues into the worker, returned so each enqueuer's grant can name its queue.

    Attributes:
        interactive: The queue `MaybeIngestPapers` puts an agent's requests on. It keeps the lane's first
            queue name, `themis-convert`, because a deleted queue's name cannot be reused for a week.
        bulk: The queue every other writer and the sweep put their requests on.
    """

    def __init__(self, *, project: str, region: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__('themis:infra:WorkQueues', 'themis', None, opts)
        # Aliased to its root-level URN, so it is updated in place rather than replaced.
        self.interactive = _queue(
            'themis-convert',
            project=project,
            region=region,
            concurrent_dispatches=_INTERACTIVE_CONCURRENT_DISPATCHES,
            opts=pulumi.ResourceOptions(parent=self, aliases=[pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)]),
        )
        self.bulk = _queue(
            'themis-convert-bulk',
            project=project,
            region=region,
            concurrent_dispatches=_BULK_CONCURRENT_DISPATCHES,
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.register_outputs({})


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


class ConvertWorker(pulumi.ComponentResource):
    """The convert worker Cloud Run service (IAM-gated ingress) and its runtime SA.

    Attributes:
        service_account_email: The runtime SA's email — object read/write on the litcache bucket, and
            the `email` claim the Anthropic federation rule matches (Path B, claude-api-wif.md).
        service_account_unique_id: The runtime SA's numeric unique id — the stable `sub` claim that
            rule pins. Never reissued, so a replacement account cannot inherit the pin.
        service_name: The Cloud Run service name, for the invoker's `run.invoker` binding.
        url: The service's `run.app` URL — the `/convert` target the enqueuer POSTs to.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        fulltext_bucket: pulumi.Input[str],
        anthropic_federation_rule_id: str,
        anthropic_organization_id: str,
        anthropic_service_account_id: str,
        anthropic_workspace_id: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:ConvertWorker', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # Dedicated runtime identity: the worker reads seed PDFs and writes back renderings/markers,
        # and is the Claude API's federation client. protect + retain_on_delete because the registered
        # federation rule pins this SA's unique_id, which is never reissued — a delete/replace would
        # strand it.
        service_account = gcp.serviceaccount.Account(
            'themis-convert-worker-runtime',
            project=project,
            account_id='themis-convert-worker',
            display_name='Themis full-text convert worker runtime',
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True, retain_on_delete=True)),
        )
        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id

        # Read the manifest and seed PDF, write back the rendering and `.fetch_outcome` marker. Read-write
        # rather than read + create: the manifest commit replaces `manifest.pb`, and a GCS replace needs
        # `objects.delete`, which objectCreator withholds.
        grants.BucketObjectReadWriter(
            'themis-convert-worker',
            member=service_account.member,
            bucket=fulltext_bucket,
            role='roles/storage.objectUser',
            target='fulltext',
            prior=grants.Prior('themis-convert-worker-fulltext', parent=self),
            opts=child,
        )
        # The request-token counter each transcription feeds (`themis/telemetry`).
        grants.TelemetryWriter('themis-convert-worker', member=service_account.member, project=project, opts=child)

        service = gcp.cloudrunv2.Service(
            'themis-convert-worker',
            project=project,
            name='themis-convert-worker',
            location=region,
            deletion_protection=False,
            # Public ingress so Cloud Tasks reaches the run.app URL with its OIDC token; IAM require-auth
            # (no invoker binding here — only ConversionInvoker's SA gets run.invoker) makes Cloud Run's
            # default-deny refuse everything else.
            ingress='INGRESS_TRAFFIC_ALL',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                # One request per instance, so the queues together never need more instances than they dispatch at
                # once.
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0,
                    max_instance_count=_INTERACTIVE_CONCURRENT_DISPATCHES + _BULK_CONCURRENT_DISPATCHES,
                ),
                # The conversion is the request (I/O-bound on the model API); Cloud Run keeps CPU
                # allocated for its duration, up to this timeout.
                timeout=_REQUEST_TIMEOUT,
                # One conversion per instance: each holds a PDF in memory and one long model stream;
                # fleet-wide parallelism is the queues' max_concurrent_dispatches, not per-instance fan-in.
                # Load-bearing beyond memory — the handler awaits blocking GCS and litdown work directly
                # on the event loop, so a value above 1 interleaves requests only at await points.
                max_instance_request_concurrency=1,
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            _env('THEMIS_FULLTEXT_BUCKET', fulltext_bucket),
                            # PDF-OCR client credentials: keyless WIF (Path B), its own svac + rule
                            # (../../docs/runbooks/claude-api-wif.md).
                            _env('ANTHROPIC_FEDERATION_RULE_ID', anthropic_federation_rule_id),
                            _env('ANTHROPIC_ORGANIZATION_ID', anthropic_organization_id),
                            _env('ANTHROPIC_SERVICE_ACCOUNT_ID', anthropic_service_account_id),
                            _env('ANTHROPIC_WORKSPACE_ID', anthropic_workspace_id),
                        ],
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(container_port=8080),  # HTTP/1.1
                        # A whole PDF in memory alongside the model request drives this; tune from
                        # observed conversion memory/CPU.
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            limits={'cpu': '2', 'memory': '4Gi'}
                        ),
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            http_get=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeHttpGetArgs(path='/healthz'),
                        ),
                    )
                ],
            ),
            opts=child,
        )
        self.service_name = service.name
        self.url = service.uri
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
                'service_name': self.service_name,
                'url': self.url,
            }
        )


class ConversionInvoker(pulumi.ComponentResource):
    """The identity a conversion task carries: its OIDC token authenticates the push to the worker.

    Attributes:
        service_account_email: The invoker SA's email — the `oidc_token` identity an enqueuer names
            on each task it creates.
        service_account_id: The invoker SA's fully-qualified resource name — the target of the
            enqueuer's `actAs` grant.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        worker_service_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:ConversionInvoker', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-convert-invoker',
            project=project,
            account_id='themis-convert-invoker',
            display_name='Themis conversion task invoker',
            opts=child,
        )
        self.service_account_email = service_account.email
        self.service_account_id = service_account.name

        # The task's OIDC identity must be authorized to invoke the require-auth worker.
        grants.ServiceInvoker(
            'themis-convert-invoker',
            member=service_account.member,
            service=worker_service_name,
            project=project,
            location=region,
            target='convert-worker',
            prior=grants.Prior('themis-convert-invoker-runs-worker', parent=self),
            opts=child,
        )

        # Cloud Tasks mints the task's OIDC token at dispatch as its service agent, acting as the invoker
        # SA. Google's guidance on the exact role the agent needs on that SA conflicts: the Cloud Tasks
        # HTTP-auth setup says serviceAccountUser (actAs), while token minting is getOpenIdToken, which
        # lives in serviceAccountTokenCreator. Grant both on this single-purpose invoker SA (negligible
        # blast radius) so dispatch works under either — a wrong single choice fails every conversion at
        # dispatch. `ServiceIdentity` provisions the agent (it may not exist until first use); cloudtasks
        # is enabled by the baseline.
        tasks_agent = gcp.projects.ServiceIdentity(
            'themis-cloudtasks-identity',
            project=project,
            service='cloudtasks.googleapis.com',
            opts=child,
        )
        grants.AccountUser(
            'cloudtasks-service-agent',
            member=tasks_agent.member,
            account=self.service_account_id,
            target='convert-invoker',
            prior=grants.Prior('themis-cloudtasks-acts-as-invoker', parent=self),
            opts=child,
        )
        grants.AccountImpersonator(
            'cloudtasks-service-agent',
            member=tasks_agent.member,
            account=self.service_account_id,
            target='convert-invoker',
            prior=grants.Prior('themis-cloudtasks-mints-token-invoker', parent=self),
            opts=child,
        )
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_id': self.service_account_id,
            }
        )
