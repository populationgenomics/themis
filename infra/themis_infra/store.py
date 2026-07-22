"""The store service: an internal Cloud Run service over two GCS buckets (docs/design/services.md).

Provisions the store data-plane service for one environment — a runtime SA, the
working-document and ephemeral-workspace buckets, the SA's object-admin on both, and
a Cloud Run service reached only through the sandbox's internal load balancer. The
container runs the `gcs` storage backend and resolves each request's session through
the auth service at `THEMIS_AUTH_URL` (self-hosted-sandbox.md §7, §9). The caller — the
sandbox proxy — dials the LB's private hostname, so the service accepts the ID token
minted for that hostname (`custom_audiences`) and admits only load-balancer ingress.
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
        service_name: The Cloud Run service name, for the load balancer's serverless NEG
            and the sandbox job's invoker binding.
        working_document_bucket: The versioned working-document bucket name.
        workspace_bucket: The ephemeral-workspace bucket name.
        url: The service's `run.app` URL (reached only through the internal load balancer).
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        auth_url: pulumi.Input[str],
        custom_audiences: pulumi.Input[list[str]],
        vpc_network: pulumi.Input[str],
        vpc_subnetwork: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:StoreService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-store-runtime',
            project=project,
            account_id='themis-store',
            display_name='Themis store service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email
        member = pulumi.Output.concat('serviceAccount:', service_account.email)

        # Working-document versions are first-class objects (<analysis>/versions/<n>),
        # not GCS object versions; the deliverable persists, retention a later policy.
        working_documents = gcp.storage.Bucket(
            'themis-store-working-documents',
            project=project,
            name=f'{project}-store-working-documents',
            location=region,
            uniform_bucket_level_access=True,
            public_access_prevention='enforced',
            opts=child,
        )
        self.working_document_bucket = working_documents.name

        workspace = gcp.storage.Bucket(
            'themis-store-workspace',
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
                f'themis-store-{label}-object-admin',
                bucket=bucket.name,
                role='roles/storage.objectAdmin',
                member=member,
                opts=child,
            )

        service = gcp.cloudrunv2.Service(
            'themis-store-service',
            project=project,
            name='themis-store',
            location=region,
            deletion_protection=False,
            # Reached only through the sandbox's internal load balancer; direct run.app calls rejected.
            ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
            # The proxy dials the LB's private hostname, so its ID token's audience is that hostname.
            custom_audiences=custom_audiences,
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                # Direct VPC egress so the auth call arrives over the VPC and auth's internal ingress
                # admits it; all traffic, since auth's run.app is a public hostname a private-ranges
                # route would send straight out. GCS stays on the private path (subnet PGA).
                vpc_access=gcp.cloudrunv2.ServiceTemplateVpcAccessArgs(
                    network_interfaces=[
                        gcp.cloudrunv2.ServiceTemplateVpcAccessNetworkInterfaceArgs(
                            network=vpc_network, subnetwork=vpc_subnetwork
                        )
                    ],
                    egress='ALL_TRAFFIC',
                ),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        # PutWorkspace buffers the archive whole and copies it once to upload; hold two
                        # copies of the cap (servicer.py) plus the GCS client and runtime.
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            limits={'cpu': '1', 'memory': '2Gi'}
                        ),
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
        self.service_name = service.name
        self.url = service.uri
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_name': self.service_name,
                'working_document_bucket': self.working_document_bucket,
                'workspace_bucket': self.workspace_bucket,
                'url': self.url,
            }
        )
