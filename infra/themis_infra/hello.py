"""The hello service: an internal Cloud Run gRPC service (docs/design/services.md).

A session-token-authed internal test consumer (sandbox-worker.md §"The hatch is the capability
boundary"). A runtime SA and an internal-ingress HTTP/2 service, resolving each request's session
through the auth service at ``THEMIS_AUTH_URL``. Reached over Direct VPC egress at the ``run.app``
URL ``cloud_run.service_url`` builds, never publicly; no load balancer.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import cloud_run, services_network

# The code-mode demo service; one instance serves every sandbox that calls it.
_MAX_INSTANCES = 1
_SERVICE_NAME = 'themis-hello'


class HelloService(pulumi.ComponentResource):
    """Cloud Run hello service (internal ingress), resolving sessions through auth.

    Attributes:
        service_account_email: The runtime SA's email — the ``run.invoker`` member on the auth service.
        service_name: The Cloud Run service name, the subject of the sandbox job's invoker binding.
        url: The service's ``run.app`` URL, built from its name: the URL a caller dials and mints its ID token
            for. It resolves once Cloud Run reports serving the service at it and the services network's record
            answers its host.
    """

    def __init__(
        self,
        *,
        project: str,
        project_number: str,
        region: str,
        image: pulumi.Input[str],
        auth_url: pulumi.Input[str],
        services_net: services_network.ServicesNetwork,
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
            name=_SERVICE_NAME,
            location=region,
            deletion_protection=False,
            # Internal ingress: reachable from the sandbox worker's Direct VPC egress at its run.app
            # URL, never publicly.
            ingress='INGRESS_TRAFFIC_INTERNAL_ONLY',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                execution_environment=cloud_run.EXECUTION_ENVIRONMENT,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0, max_instance_count=_MAX_INSTANCES
                ),
                # Direct VPC egress on the services network, private ranges only (services_network.py).
                vpc_access=gcp.cloudrunv2.ServiceTemplateVpcAccessArgs(
                    network_interfaces=[
                        gcp.cloudrunv2.ServiceTemplateVpcAccessNetworkInterfaceArgs(
                            network=services_net.network, subnetwork=services_net.subnetwork
                        )
                    ],
                    egress='PRIVATE_RANGES_ONLY',
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
                        startup_probe=cloud_run.grpc_startup_probe(8080),
                    )
                ],
            ),
            opts=child,
        )
        self.service_name = service.name
        self.url = services_net.internal_url(
            service,
            name=_SERVICE_NAME,
            url=cloud_run.service_url(name=_SERVICE_NAME, project_number=project_number, region=region),
        )
        self.register_outputs(
            {'service_account_email': self.service_account_email, 'service_name': self.service_name, 'url': self.url}
        )
