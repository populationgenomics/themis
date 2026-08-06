"""The evidence service: the literature read surface as an internal Cloud Run gRPC service.

The workbench document pane's backend (docs/design/document-pane.md, docs/design/services.md): a
runtime SA and an HTTP/2 gRPC service that reads the litcache fulltext bucket (object-viewer,
read-only) to resolve a paper's rendering / PDF / associated-file object and locate a quote. The
`THEMIS_BACKEND=live` litcache backend; no auth service, no Cloud SQL — a `doc_id` is the litcache
canonical id and the corpus is not session-scoped (entitlement is a deferred non-goal).

Its callers are the web BFF — quote location for the document pane, plus some paper management — and,
the primary consumer, the sandbox agents (reading a paper's full text and validating quotes at authoring
time). The BFF has no Direct VPC egress (it reaches GCS / Cloud SQL / Anthropic over Google APIs, not the
services VPC), so an internal-ingress service would be unreachable from it.
Ingress is therefore `ALL` gated by IAM, with no `run.invoker` binding yet: the BFF that calls it is
not wired in this component. Its `run.invoker` (audience = this service's URL) and its object-viewer on
the fulltext bucket — the BFF reads/serves the resolved object — attach with the BFF. Until then the
URL is resolvable but every request is refused, Cloud Run's default-deny with no invoker member.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


class EvidenceService(pulumi.ComponentResource):
    """Cloud Run evidence service (IAM-gated ingress): litcache content resolution + quote location.

    Attributes:
        service_account_email: The runtime SA's email — the object-viewer on the fulltext bucket.
        service_name: The Cloud Run service name, for the web SA's invoker binding.
        url: The service's ``run.app`` URL — the audience the BFF's ID-token interceptor mints for.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        fulltext_bucket: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:EvidenceService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-evidence-runtime',
            project=project,
            account_id='themis-evidence',
            display_name='Themis evidence service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email

        # Read-only on the litcache fulltext bucket: the backend reads manifests + renderings to
        # resolve objects and locate quotes; it never writes (the cache warms via ingestion).
        gcp.storage.BucketIAMMember(
            'themis-evidence-fulltext-object-viewer',
            bucket=fulltext_bucket,
            role='roles/storage.objectViewer',
            member=pulumi.Output.concat('serviceAccount:', service_account.email),
            opts=child,
        )

        service = gcp.cloudrunv2.Service(
            'themis-evidence-service',
            project=project,
            name='themis-evidence',
            location=region,
            deletion_protection=False,
            # IAM-gated public ingress: the BFF (no VPC egress) reaches the run.app URL with an ID
            # token. No invoker binding here — it attaches with the BFF; until then Cloud Run's
            # default-deny (no invoker member) refuses every request.
            ingress='INGRESS_TRAFFIC_ALL',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name='THEMIS_BACKEND', value='live'),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_FULLTEXT_BUCKET', value=fulltext_bucket
                            ),
                        ],
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(name='h2c', container_port=8080),
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            grpc=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeGrpcArgs(port=8080),
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
                'service_name': self.service_name,
                'url': self.url,
            }
        )
