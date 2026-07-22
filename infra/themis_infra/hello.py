"""The hello service: an internal Cloud Run gRPC service (docs/design/services.md).

The sandbox forward leg's session-token-authed test consumer (self-hosted-sandbox.md §6). A runtime SA
and an HTTP/2 service reached only through the sandbox's internal load balancer, resolving each
request's session through the auth service at ``THEMIS_AUTH_URL``. The caller — the sandbox proxy's
forward route — dials the LB's private hostname, so the service accepts the ID token minted for that
hostname (``custom_audiences``) and admits only load-balancer ingress. Its invoker binding to the
sandbox job SA attaches in the program entrypoint.
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
        custom_audiences: pulumi.Input[list[str]],
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
            # Reached only through the sandbox's internal load balancer; direct run.app calls rejected.
            ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
            # The proxy dials the LB's private hostname, so its ID token's audience is that hostname.
            custom_audiences=custom_audiences,
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
