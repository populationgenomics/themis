"""The on-demand full-text conversion lane: the Cloud Tasks queue and the pushed convert worker.

Architecture B (`docs/design/evidence-fulltext.md`): all fetch/convert work lives in a separate worker,
so the evidence read image stays lean. A Cloud Task pushes `POST /convert {"doc_id"}` to the worker,
which runs the litcache producer (OA XML → markdown, else PDF LLM-OCR) off any request path.

The evidence service is the producer: `MaybeIngestPapers` creates one `doc_id`-named task per paper it
resolved to PENDING. Its two grants on this lane are the program's, since neither the queue nor the
invoker knows its caller. The bulk ingestion pipeline puts nothing here — it commits the manifest last
with its renderings in it, so a paper it ingested is READY the moment it exists.

- `conversion_queue` — the Cloud Tasks queue. Its concurrency cap is the load-bearing knob (each
  dispatch is a model-cost-bearing conversion); bounded retries stop a permanently-failing paper
  rather than re-OCR it forever.
- `ConvertWorker` — the worker Cloud Run service (IAM-gated, require-auth) and its runtime SA
  (object read/write on the litcache bucket; the Claude API by workload identity federation).
- `ConversionInvoker` — the identity whose OIDC token the task carries: `run.invoker` on the worker,
  and the Cloud Tasks service agent may mint its token. Whatever enqueues needs `actAs` on it.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# The maximum concurrent conversions the queue dispatches. The knob that matters: each dispatch runs a
# conversion that may call a model API, so this caps in-flight model cost, not just load. Tune
# against observed conversion volume and spend.
_MAX_CONCURRENT_DISPATCHES = 5
# Bounded retries: a raise → 500 → retry re-runs the (model-cost-bearing) conversion, so a
# permanently-unconvertible paper stops after this many attempts rather than re-OCRing forever. Cloud
# Tasks has no dead-letter destination — an exhausted task is simply deleted, leaving the paper PENDING
# with no marker and no record, which only a scan over corpus state re-finds. 1 attempt + 4 retries at a
# doubling 30s spreads a transient failure (model-API overload, a fetch blip) over ~7.5 minutes; the
# ceiling binds only if the attempt count is raised.
_MAX_ATTEMPTS = 5
_MIN_BACKOFF = '30s'
_MAX_BACKOFF = '600s'
# The per-conversion request timeout — Cloud Tasks' 30-minute dispatch-deadline maximum (the enqueuer
# sets a matching per-task deadline), so a conversion up to 30 min is never abandoned and retried
# concurrently. A PDF beyond this ceiling is the design's open question (it would need a Job).
_REQUEST_TIMEOUT = '1800s'


def conversion_queue(
    *,
    project: str,
    region: str,
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.cloudtasks.Queue:
    """Create the conversion Cloud Tasks queue, returned so the producer's enqueuer grant can name it."""
    return gcp.cloudtasks.Queue(
        'themis-convert',
        project=project,
        location=region,
        name='themis-convert',
        rate_limits=gcp.cloudtasks.QueueRateLimitsArgs(max_concurrent_dispatches=_MAX_CONCURRENT_DISPATCHES),
        retry_config=gcp.cloudtasks.QueueRetryConfigArgs(
            max_attempts=_MAX_ATTEMPTS,
            min_backoff=_MIN_BACKOFF,
            max_backoff=_MAX_BACKOFF,
        ),
        opts=opts,
    )


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

        # Read the manifest and seed PDF, write back the rendering and `.fetch_outcome` marker.
        # objectUser, not objectViewer+objectCreator: the manifest commit replaces `manifest.pb`,
        # and a GCS replace needs `objects.delete`, which objectCreator withholds.
        gcp.storage.BucketIAMMember(
            'themis-convert-worker-fulltext',
            bucket=fulltext_bucket,
            role='roles/storage.objectUser',
            member=service_account.member,
            opts=child,
        )

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
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                # The conversion is the request (I/O-bound on the model API); Cloud Run keeps CPU
                # allocated for its duration, up to this timeout.
                timeout=_REQUEST_TIMEOUT,
                # One conversion per instance: each holds a PDF in memory and one long model stream;
                # fleet-wide parallelism is the queue's max_concurrent_dispatches, not per-instance fan-in.
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
        gcp.cloudrunv2.ServiceIamMember(
            'themis-convert-invoker-runs-worker',
            project=project,
            location=region,
            name=worker_service_name,
            role='roles/run.invoker',
            member=service_account.member,
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
        for slug, role in (
            ('acts-as', 'roles/iam.serviceAccountUser'),
            ('mints-token', 'roles/iam.serviceAccountTokenCreator'),
        ):
            gcp.serviceaccount.IAMMember(
                f'themis-cloudtasks-{slug}-invoker',
                service_account_id=self.service_account_id,
                role=role,
                member=tasks_agent.member,
                opts=child,
            )
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_id': self.service_account_id,
            }
        )
