"""The web service: Cloud Run behind an external HTTPS load balancer and IAP.

Provisions the public web surface for one environment — a Cloud Run service, the
external Application Load Balancer that fronts it (serverless NEG, Google-managed
TLS certificate, HTTP→HTTPS redirect), and IAP enforcing the access group on the
backend. Identical across environments; all per-environment values arrive as
constructor arguments.

The load balancer needs a stable address before DNS can point at it, so the
component reserves a global static IP and exposes it as `ip_address`; the
hostname's A record is added out of band (see ../README.md). Until the record
resolves, the managed certificate stays PROVISIONING.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Coarse "may reach the app" gate granted to the access group on the IAP
# resource — not an application role (those live in the app).
_IAP_ACCESSOR_ROLE = 'roles/iap.httpsResourceAccessor'


class WebService(pulumi.ComponentResource):
    """Cloud Run web service fronted by an external HTTPS LB with IAP.

    Attributes:
        ip_address: The load balancer's reserved global IP. The environment's
            hostname A record points here (added out of band).
        url: The service URL once DNS and the certificate are live.
        service_account_email: The runtime SA's email — the `email` claim the
            Anthropic WIF federation pins (the web app is the Managed-Agents
            client; see ../../docs/runbooks/claude-api-wif.md).
        service_account_unique_id: The runtime SA's numeric unique ID — the
            stable `sub` claim the federation pins (never reused, so it survives
            a delete/recreate of the same email).
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        domain: str,
        image: pulumi.Input[str],
        iap_member: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:WebService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # Dedicated runtime identity for the web app — it is the Managed-Agents
        # (Anthropic) client and reads display rows from Cloud SQL. The Anthropic
        # WIF federation pins this SA's email/unique_id
        # (../../docs/runbooks/claude-api-wif.md); Anthropic and Cloud SQL read
        # grants attach here as they land.
        service_account = gcp.serviceaccount.Account(
            'themis-runtime',
            project=project,
            account_id='themis-web',
            display_name='Themis web service runtime (Managed Agents client)',
            opts=child,
        )
        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id

        self._service = gcp.cloudrunv2.Service(
            'themis-service',
            project=project,
            # Explicit, stable service name (referenced by the deploy/preview
            # workflows and the console) — not an auto-generated one.
            name='themis-web',
            location=region,
            # Network gate: only the external LB (and internal traffic) may
            # reach the service — direct public requests to the run.app URL are
            # rejected here, before IAM. Paired with the IAP-service-agent-only
            # invoker below (the identity gate), IAP is the sole access path.
            ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                # Scale to zero — idle cost ≈ 0 for the spike's traffic.
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                containers=[gcp.cloudrunv2.ServiceTemplateContainerArgs(image=image)],
            ),
            opts=child,
        )

        # IAP invokes Cloud Run as its own service agent (the
        # `X-Serverless-Authorization` flow), so that agent must exist and hold
        # run.invoker on the service — otherwise IAP returns "The IAP service
        # account is not provisioned". `ServiceIdentity` provisions the agent
        # (equivalent to `gcloud beta services identity create
        # --service=iap.googleapis.com`); iap.googleapis.com is enabled by the
        # baseline this component depends on.
        iap_agent = gcp.projects.ServiceIdentity(
            'themis-iap-identity',
            project=project,
            service='iap.googleapis.com',
            opts=child,
        )

        # Identity gate: only the IAP service agent may invoke — not allUsers.
        # With ingress locked to the LB (above), a request that reaches the
        # service must still authenticate as this agent, closing the
        # unauthenticated internal-VPC path that ingress alone leaves open.
        gcp.cloudrunv2.ServiceIamMember(
            'themis-invoker',
            project=project,
            location=region,
            name=self._service.name,
            role='roles/run.invoker',
            member=iap_agent.member,
            opts=child,
        )

        self.ip_address = self._build_load_balancer('themis', project, region, domain, iap_member, child)
        self.url = pulumi.Output.format('https://{0}', domain)
        self.register_outputs(
            {
                'ip_address': self.ip_address,
                'url': self.url,
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
            }
        )

    def _build_load_balancer(
        self,
        name: str,
        project: str,
        region: str,
        domain: str,
        iap_member: pulumi.Input[str],
        child: pulumi.ResourceOptions,
    ) -> pulumi.Output[str]:
        """Build the external HTTPS load balancer chain and return its IP."""
        # The environment's DNS A record (added out of band) points at this IP,
        # so it must stay constant across deploys. `protect` makes Pulumi refuse
        # to delete it — turning an accidental destroy or replace into a loud
        # error rather than a silent address swap — and the explicit name lets
        # it be recovered by `pulumi import` if state is ever lost. None of its
        # replace-triggering fields change on a normal `up`/`refresh`.
        address = gcp.compute.GlobalAddress(
            f'{name}-ip',
            project=project,
            name=f'{name}-ip',
            address_type='EXTERNAL',
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True)),
        )

        neg = gcp.compute.RegionNetworkEndpointGroup(
            f'{name}-neg',
            project=project,
            region=region,
            network_endpoint_type='SERVERLESS',
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=self._service.name),
            opts=child,
        )

        backend = gcp.compute.BackendService(
            f'{name}-backend',
            project=project,
            protocol='HTTPS',
            load_balancing_scheme='EXTERNAL_MANAGED',
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            # Google-managed OAuth client — no client id/secret to store
            # (see docs/design/deployment.md §4). IAP is the authentication gate.
            iap=gcp.compute.BackendServiceIapArgs(enabled=True),
            opts=child,
        )

        # Grant the access group "may reach the app" on the IAP-protected backend.
        gcp.iap.WebBackendServiceIamMember(
            f'{name}-iap-access',
            project=project,
            web_backend_service=backend.name,
            role=_IAP_ACCESSOR_ROLE,
            member=iap_member,
            opts=child,
        )

        certificate = gcp.compute.ManagedSslCertificate(
            f'{name}-cert',
            project=project,
            managed=gcp.compute.ManagedSslCertificateManagedArgs(domains=[domain]),
            opts=child,
        )
        url_map = gcp.compute.URLMap(f'{name}-urlmap', project=project, default_service=backend.id, opts=child)
        https_proxy = gcp.compute.TargetHttpsProxy(
            f'{name}-https-proxy',
            project=project,
            url_map=url_map.id,
            ssl_certificates=[certificate.id],
            opts=child,
        )
        gcp.compute.GlobalForwardingRule(
            f'{name}-https',
            project=project,
            target=https_proxy.id,
            ip_address=address.address,
            port_range='443',
            load_balancing_scheme='EXTERNAL_MANAGED',
            opts=child,
        )

        _build_http_redirect(name, project, address, child)
        return address.address


def _build_http_redirect(
    name: str,
    project: str,
    address: gcp.compute.GlobalAddress,
    child: pulumi.ResourceOptions,
) -> None:
    """Redirect plain HTTP on the same IP to HTTPS (301)."""
    redirect_map = gcp.compute.URLMap(
        f'{name}-http-redirect',
        project=project,
        default_url_redirect=gcp.compute.URLMapDefaultUrlRedirectArgs(
            https_redirect=True,
            redirect_response_code='MOVED_PERMANENTLY_DEFAULT',
            strip_query=False,
        ),
        opts=child,
    )
    http_proxy = gcp.compute.TargetHttpProxy(
        f'{name}-http-proxy',
        project=project,
        url_map=redirect_map.id,
        opts=child,
    )
    gcp.compute.GlobalForwardingRule(
        f'{name}-http',
        project=project,
        target=http_proxy.id,
        ip_address=address.address,
        port_range='80',
        load_balancing_scheme='EXTERNAL_MANAGED',
        opts=child,
    )
