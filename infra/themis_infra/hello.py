"""The hello service: an internal Cloud Run gRPC service (docs/design/services.md).

A session-token-authed internal test consumer (postern-sandbox-swap.md §2). A runtime SA and an
internal-ingress HTTP/2 service, resolving each request's session through the auth service at
``THEMIS_AUTH_URL``. Reached over Direct VPC egress at its ``run.app`` URL (the default ID-token
audience), never publicly; no load balancer.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


class HelloService(pulumi.ComponentResource):
    """Cloud Run hello service (load-balancer ingress), resolving sessions through auth.

    Attributes:
        service_account_email: The runtime SA's email — the ``run.invoker`` member on the auth service.
        service_name: The Cloud Run service name, for the load balancer's serverless NEG and the
            sandbox job's invoker binding.
        url: The service's ``run.app`` URL (reached only through the internal load balancer).
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
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:HelloService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-hello-runtime',
            project=project,
            account_id='themis-hello',
            display_name='Themis hello service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email

        service = gcp.cloudrunv2.Service(
            'themis-hello-service',
            project=project,
            name='themis-hello',
            location=region,
            deletion_protection=False,
            # Internal ingress: reachable from the sandbox worker's Direct VPC egress at its run.app
            # URL (the default audience id_token mints), never publicly.
            ingress='INGRESS_TRAFFIC_INTERNAL_ONLY',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                # Direct VPC egress so the auth call arrives over the VPC and auth's internal ingress
                # admits it; all traffic, since auth's run.app is a public hostname a private-ranges
                # route would send straight out.
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
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_AUTHORIZER_BACKEND', value='http'
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name='THEMIS_AUTH_URL', value=auth_url),
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
            {'service_account_email': self.service_account_email, 'service_name': self.service_name, 'url': self.url}
        )
