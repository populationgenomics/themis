"""The store service: an internal Cloud Run service over two GCS buckets (docs/design/services.md).

Provisions the store data-plane service for one environment — a runtime SA, the
working-document and ephemeral-workspace buckets, the SA's object-admin on both, and
an internal-ingress Cloud Run service. The container runs the `gcs` storage backend
and resolves each request's session through the auth service at `THEMIS_AUTH_URL`
(self-hosted-sandbox.md §7, §9). Ingress is internal-only with no invoker binding yet
— the sandbox proxy that calls it does not exist; its `run.invoker` attaches when it
lands.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Scratch no live session touches ages out; a live session rewrites it each turn,
# resetting the age (self-hosted-sandbox.md §9).
_WORKSPACE_TTL_DAYS = 30


class StoreService(pulumi.ComponentResource):
    """Cloud Run store service (internal ingress) over the working-document + workspace buckets.

    Attributes:
        service_account_email: The runtime SA's email — the `run.invoker` member on
            the auth service, and the object-admin on both buckets.
        working_document_bucket: The versioned working-document bucket name.
        workspace_bucket: The ephemeral-workspace bucket name.
        url: The service's `run.app` URL (internal ingress).
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        auth_url: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:StoreService', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            f'{name}-store-runtime',
            project=project,
            account_id=f'{name}-store',
            display_name='Themis store service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email
        member = pulumi.Output.concat('serviceAccount:', service_account.email)

        # Working-document versions are first-class objects (<analysis>/versions/<n>),
        # not GCS object versions; the deliverable persists, retention a later policy.
        working_documents = gcp.storage.Bucket(
            f'{name}-store-working-documents',
            project=project,
            name=f'{project}-store-working-documents',
            location=region,
            uniform_bucket_level_access=True,
            public_access_prevention='enforced',
            opts=child,
        )
        self.working_document_bucket = working_documents.name

        workspace = gcp.storage.Bucket(
            f'{name}-store-workspace',
            project=project,
            name=f'{project}-store-workspace',
            location=region,
            uniform_bucket_level_access=True,
            public_access_prevention='enforced',
            lifecycle_rules=[
                gcp.storage.BucketLifecycleRuleArgs(
                    action=gcp.storage.BucketLifecycleRuleActionArgs(type='Delete'),
                    condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=_WORKSPACE_TTL_DAYS),
                )
            ],
            opts=child,
        )
        self.workspace_bucket = workspace.name

        # Read/write on both buckets; the store derives every key server-side, so
        # object-admin scoped to these buckets is the whole credential (§7).
        for label, bucket in (('working-documents', working_documents), ('workspace', workspace)):
            gcp.storage.BucketIAMMember(
                f'{name}-store-{label}-object-admin',
                bucket=bucket.name,
                role='roles/storage.objectAdmin',
                member=member,
                opts=child,
            )

        service = gcp.cloudrunv2.Service(
            f'{name}-store-service',
            project=project,
            name=f'{name}-store',
            location=region,
            # Internal only: called service-to-service (the sandbox proxy, later),
            # never from the public internet.
            ingress='INGRESS_TRAFFIC_INTERNAL_ONLY',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name='THEMIS_STORAGE_BACKEND', value='gcs'),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_AUTHORIZER_BACKEND', value='http'
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_STORE_WORKING_DOCUMENT_BUCKET', value=working_documents.name
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_STORE_WORKSPACE_BUCKET', value=workspace.name
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name='THEMIS_AUTH_URL', value=auth_url),
                        ],
                        # Serve gRPC: a named `h2c` port makes Cloud Run speak HTTP/2 cleartext
                        # to the container (TLS terminated at the ingress), and the startup probe
                        # checks the grpc.health.v1 service the server registers.
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(name='h2c', container_port=8080),
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            grpc=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeGrpcArgs(port=8080),
                        ),
                    )
                ],
            ),
            opts=child,
        )
        self.url = service.uri
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'working_document_bucket': self.working_document_bucket,
                'workspace_bucket': self.workspace_bucket,
                'url': self.url,
            }
        )
