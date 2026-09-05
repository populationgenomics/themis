"""The sheaf service: the Cloud Run gRPC deployment of the `Sheaf` interface (docs/design/sheaf-service.md).

A runtime SA and an HTTP/2 gRPC service running the `gcs` backend over the store's workspace bucket
(the store component's; this one holds `roles/storage.objectUser` on it, not the bucket itself) and
resolving each request's session through the auth service at `THEMIS_AUTH_URL`. Ingress is `ALL`, gated
by IAM: the invoker bindings are the program's (`infra/__main__.py`), one per caller, and default-deny
refuses any other. What the service needs of its host, and why: sheaf-service.md § Deployment.

The three ceilings the servicer enforces are this module's constants — deployment configuration — and
the memory limit and per-instance concurrency are sized against the publish ceiling.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# What a caller may publish in one call, and what a publish may leave behind. The document ceiling sits
# under gRPC's default 4 MiB message limit, since `ReadRefDoc` returns the document in one message.
_MAX_PUBLISH_BYTES = 256 * 1024 * 1024
_MAX_REFS = 10_000
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# A ceiling-sized publish over a 5 Mbit/s uplink is ~7 minutes of upload, and a refused one is drained to
# twice that before its status is sent; Cloud Run's 300 s default cuts both off.
_REQUEST_TIMEOUT = '1800s'
_MAX_INSTANCES = 10


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


class SheafService(pulumi.ComponentResource):
    """Cloud Run sheaf service (IAM-gated ingress) over the store's workspace bucket.

    Attributes:
        service_account_email: The runtime SA's email — the `run.invoker` member on the auth service,
            and the object-user on the workspace bucket.
        service_name: The Cloud Run service name, the subject of the program's invoker bindings.
        url: The service's ``run.app`` URL — the audience a caller's ID-token interceptor mints for.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        auth_url: pulumi.Input[str],
        vpc_network: pulumi.Input[str],
        vpc_subnetwork: pulumi.Input[str],
        workspace_bucket: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:SheafService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-sheaf-runtime',
            project=project,
            account_id='themis-sheaf',
            display_name='Themis sheaf service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email

        gcp.storage.BucketIAMMember(
            'themis-sheaf-workspace-object-user',
            bucket=workspace_bucket,
            role='roles/storage.objectUser',
            member=pulumi.Output.concat('serviceAccount:', service_account.email),
            opts=child,
        )

        service = gcp.cloudrunv2.Service(
            'themis-sheaf-service',
            project=project,
            name='themis-sheaf',
            location=region,
            deletion_protection=False,
            # IAM-gated public ingress; each caller's `run.invoker` is granted in the program.
            ingress='INGRESS_TRAFFIC_ALL',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0, max_instance_count=_MAX_INSTANCES
                ),
                timeout=_REQUEST_TIMEOUT,
                # Publish holds one pack as its chunks and again as their join before storing it — twice
                # `_MAX_PUBLISH_BYTES` (512 MiB) peak per request — so three in flight fit the 2Gi limit
                # with the GCS client and runtime; Cloud Run's default of 80 would not.
                max_instance_request_concurrency=3,
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
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            # Setting `resources` at all deploys cpuIdle false unless it is stated;
                            # a container with no resources block gets true.
                            cpu_idle=True,
                            limits={'cpu': '1', 'memory': '2Gi'},
                        ),
                        envs=[
                            _env('THEMIS_AUTHORIZER_BACKEND', 'http'),
                            _env('THEMIS_AUTH_URL', auth_url),
                            _env('THEMIS_SHEAF_BACKEND', 'gcs'),
                            _env('THEMIS_WORKSPACE_BUCKET', workspace_bucket),
                            _env('THEMIS_SHEAF_MAX_PUBLISH_BYTES', str(_MAX_PUBLISH_BYTES)),
                            _env('THEMIS_SHEAF_MAX_REFS', str(_MAX_REFS)),
                            _env('THEMIS_SHEAF_MAX_DOCUMENT_BYTES', str(_MAX_DOCUMENT_BYTES)),
                        ],
                        # Serve gRPC: a named `h2c` port makes Cloud Run speak HTTP/2 cleartext to the
                        # container (TLS terminated at the ingress), and the startup probe checks the
                        # grpc.health.v1 service the server registers.
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
            {'service_account_email': self.service_account_email, 'service_name': self.service_name, 'url': self.url}
        )
