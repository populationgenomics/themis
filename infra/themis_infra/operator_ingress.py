"""The way in for an operator calling a data-plane gRPC service from a laptop: the web load balancer, behind IAP.

The data-plane services take traffic only from inside the project (`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`):
their callers reach the `run.app` URL over the services VPC. A laptop is not on that VPC, so each service an
operator calls gets an `OperatorBackend` here, and the program routes the service's gRPC method paths to it
on the web load balancer (`web.GrpcRoute`). IAP on the backend service is the gate; who passes it is the
program's `grants.IapBackendAccessor` call sites (docs/runbooks/hand-driving-a-service.md).
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants


class OperatorBackend(pulumi.ComponentResource):
    """One Cloud Run service's serverless NEG and the IAP-enabled backend service in front of it.

    IAP admits a request only from an accessor granted on `backend_service_name`. It then calls the service
    as IAP's service agent (`X-Serverless-Authorization`), which this component grants `run.invoker`, and
    passes the caller's own `authorization` header through untouched, for the service to verify as its own.
    The load balancer ends a request to a serverless NEG after 60 minutes, whatever the service's timeout.

    Attributes:
        backend_service: The backend service's id, for the web load balancer's URL map.
        backend_service_name: The backend service's name: the IAP resource an accessor grant is over.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        service: pulumi.Input[str],
        target: str,
        iap_service_agent: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Front the Cloud Run service named `service` with an IAP-enabled backend service.

        Args:
            name: Resource-name prefix (`themis-evidence`).
            project: The GCP project.
            region: The service's region.
            service: The Cloud Run service's name.
            target: A slug for the service, for the invoker grant's name (`evidence`).
            iap_service_agent: The IAP service agent's IAM member.
            opts: Resource options.
        """
        super().__init__('themis:infra:OperatorBackend', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        invoker = grants.ServiceInvoker(
            'iap-service-agent',
            member=iap_service_agent,
            service=service,
            project=project,
            location=region,
            target=target,
            opts=child,
        )
        neg = gcp.compute.RegionNetworkEndpointGroup(
            f'{name}-operator-neg',
            project=project,
            region=region,
            network_endpoint_type='SERVERLESS',
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service),
            opts=child,
        )
        backend = gcp.compute.BackendService(
            f'{name}-operator-backend',
            project=project,
            # A serverless NEG ignores the protocol; Cloud Run speaks HTTP/2 to the container for gRPC.
            protocol='HTTPS',
            load_balancing_scheme='EXTERNAL_MANAGED',
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            # No OAuth client: IAP uses its Google-managed one.
            iap=gcp.compute.BackendServiceIapArgs(enabled=True),
            log_config=gcp.compute.BackendServiceLogConfigArgs(enable=True, sample_rate=1.0),
            # The URL map routes here only once IAP can call the service.
            opts=pulumi.ResourceOptions(parent=self, depends_on=[invoker]),
        )
        self.backend_service = backend.id
        self.backend_service_name = backend.name
        self.register_outputs(
            {'backend_service': self.backend_service, 'backend_service_name': self.backend_service_name}
        )
