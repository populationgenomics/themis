"""The web service: Cloud Run with IAP, behind an external HTTPS load balancer.

Provisions the public web surface for one environment — a Cloud Run service with
IAP enabled on it, and the external Application Load Balancer that fronts it
(serverless NEG, Google-managed TLS certificate, HTTP→HTTPS redirect). The load
balancer also carries the routes other components hand it, each sent to that
component's own backend service: a `Route` is one exact path, a `GrpcRoute` every
method of one gRPC service. Identical across environments; all per-environment
values arrive as constructor arguments.

The web tier attaches to the services network with Direct VPC egress, because the
evidence and sheaf services it calls take only internal traffic.

The load balancer needs a stable address before DNS can point at it, so the
component reserves a global static IP and exposes it as `ip_address`; the
hostname's A record is added out of band (see ../README.md). Until the record
resolves, the managed certificate stays PROVISIONING.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants, sql

# Each instance pools up to 5 Cloud SQL connections, so 2 hold at most 10 of db-f1-micro's 25 (auth holds 12).
_MAX_INSTANCES = 2

# The service's own name is also an input to its IAP-assertion audience, so it is a
# literal rather than the resource's output, which the template cannot read.
_SERVICE_NAME = 'themis-web'


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


@dataclasses.dataclass(frozen=True)
class Route:
    """One path on the web hostname that the load balancer sends to another backend service.

    The backend service brings its own access control: a routed request bypasses the web service,
    and with it IAP, which is enabled on the web Cloud Run service rather than on the load balancer.

    Attributes:
        path: The exact request path, without a wildcard; every other path stays on the web service.
        backend_service: The id of the global backend service the path is sent to.
    """

    path: str
    backend_service: pulumi.Input[str]

    def __post_init__(self) -> None:
        if not self.path.startswith('/') or any(char in self.path for char in '*?#'):
            raise ValueError(f'route path {self.path!r} must be an exact path starting with /')

    @property
    def url_map_path(self) -> str:
        """The path as the URL map's path rule matches it."""
        return self.path


# `<package>.<Service>`: lower-case dotted package segments, then one capitalised service segment.
_GRPC_SERVICE = re.compile(r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.[A-Z][A-Za-z0-9]*')


@dataclasses.dataclass(frozen=True)
class GrpcRoute:
    """Every method of one gRPC service on the web hostname, sent to another backend service.

    A gRPC call's path is `/<package>.<Service>/<Method>`, so the route matches the prefix `/<package>.<Service>/`
    and nothing outside it. A path under the prefix that names no method of the service still reaches the backend,
    which answers `UNIMPLEMENTED`. As with `Route`, the backend service brings its own access control.

    Attributes:
        service: The fully-qualified gRPC service name the proto declares (`themis.rpc.sheaf.Sheaf`).
        backend_service: The id of the global backend service the service's methods are sent to.
    """

    service: str
    backend_service: pulumi.Input[str]

    def __post_init__(self) -> None:
        if not _GRPC_SERVICE.fullmatch(self.service):
            raise ValueError(
                f'gRPC route {self.service!r} must be a fully-qualified gRPC service name (themis.rpc.sheaf.Sheaf)'
            )

    @property
    def prefix(self) -> str:
        """The path prefix every method of the service shares."""
        return f'/{self.service}/'

    @property
    def url_map_path(self) -> str:
        """The prefix as the URL map's path rule matches it: a trailing wildcard."""
        return f'{self.prefix}*'


def check_routes(routes: Sequence[Route | GrpcRoute]) -> None:
    """Raise unless each route matches paths no other route matches.

    The URL map sends a request to its longest matching path rule, so an exact path under a gRPC route's prefix
    would take that one method away from the gRPC route's backend, and with it that backend's access control.

    Raises:
        ValueError: If two routes share a path, or an exact path falls under a gRPC route's prefix.
    """
    paths = [route.url_map_path for route in routes]
    if len(set(paths)) != len(paths):
        raise ValueError(f'duplicate route paths: {sorted(paths)}')
    prefixes = tuple(route.prefix for route in routes if isinstance(route, GrpcRoute))
    shadowed = sorted(route.path for route in routes if isinstance(route, Route) and route.path.startswith(prefixes))
    if shadowed:
        raise ValueError(f'exact route paths under a gRPC route prefix: {shadowed}')


class WebService(pulumi.ComponentResource):
    """Cloud Run web service with IAP, fronted by an external HTTPS LB.

    Attributes:
        ip_address: The load balancer's reserved global IP. The environment's
            hostname A record points here (added out of band).
        url: The service URL once DNS and the certificate are live.
        service_account_email: The runtime SA's email — the `email` claim the
            Anthropic WIF federation pins (the web app is the Managed-Agents
            client; see ../../docs/runbooks/claude-api-wif.md).
        service_account_unique_id: The runtime SA's numeric unique ID — the
            stable `sub` claim the federation pins; never reissued, so a
            recreated account with the same email does not match the rule.
        db_user: The web SA's Cloud SQL IAM DB-user login — `THEMIS_DB_USER`
            for the container, and the `${WEB_DB_USER}` the `analyses` and
            `session_context` write grants substitute.
        service_name: The Cloud Run service's name — the IAP resource an
            `IapAccessor` grant is over. Who may reach the app is decided in the
            program, not here.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        domain: str,
        image: pulumi.Input[str],
        sql_instance: gcp.sql.DatabaseInstance,
        sql_connection_name: pulumi.Input[str],
        sql_database: pulumi.Input[str],
        session_token_key_version: pulumi.Input[str],
        evidence_url: pulumi.Input[str],
        fulltext_bucket: pulumi.Input[str],
        sheaf_url: pulumi.Input[str],
        sheaf_bucket: pulumi.Input[str],
        anthropic_environment_id: str,
        anthropic_agent_id: str,
        anthropic_federation_rule_id: str,
        anthropic_organization_id: str,
        anthropic_service_account_id: str,
        anthropic_workspace_id: str,
        project_number: pulumi.Input[str],
        trace_sample_ratio: float,
        iap_service_agent: pulumi.Input[str],
        vpc_network: pulumi.Input[str],
        vpc_subnetwork: pulumi.Input[str],
        routes: Sequence[Route | GrpcRoute],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:WebService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # Dedicated runtime identity: the web app is the Managed-Agents (Anthropic)
        # client and the Cloud SQL writer. The Anthropic WIF federation pins this SA's
        # email/unique_id (../../docs/runbooks/claude-api-wif.md); the unique_id is never
        # reissued, so protect + retain_on_delete refuse a delete/replace that would
        # strand the pinned rule. account_id/project (the replace triggers) don't change
        # on a normal `up`.
        service_account = gcp.serviceaccount.Account(
            'themis-runtime',
            project=project,
            account_id='themis-web',
            display_name='Themis web service runtime (Managed Agents client)',
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True, retain_on_delete=True)),
        )
        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id

        # The BFF 302s the paper-content routes to V4 read URLs (document-pane.md §Backend seam); the signed
        # URL reads as the SA's bucket grants.
        grants.SelfSigner(
            'themis-web',
            account=service_account,
            prior=grants.Prior('themis-web-sign-blob', parent=self),
            opts=child,
        )

        # The SA's Cloud SQL IAM DB-user login and its connect roles; the analyses and
        # session_context write grants are applied by the migration, keyed on this login.
        db_user = sql.iam_db_user(
            'themis-web',
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            opts=child,
        )
        grants.DatabaseConnector(
            'themis-web',
            member=grants.service_account(service_account.email),
            project=project,
            prior=grants.Prior('themis-web', parent=self),
            opts=child,
        )
        self.db_user = db_user.name
        grants.TraceWriter(
            'themis-web', member=grants.service_account(service_account.email), project=project, opts=child
        )

        self._service = gcp.cloudrunv2.Service(
            'themis-service',
            project=project,
            # Explicit, stable service name (referenced by the deploy/preview
            # workflows and the console) — not an auto-generated one.
            name=_SERVICE_NAME,
            location=region,
            deletion_protection=False,
            # IAP runs in front of the service on every ingress path, the load
            # balancer's included; it cannot also be on the LB's backend service.
            # Google-managed OAuth client (../../docs/design/spike-infrastructure.md §4).
            iap_enabled=True,
            # Network gate: only the external LB (and internal traffic) may
            # reach the service — direct public requests to the run.app URL are
            # rejected here, before IAP and IAM. Paired with the IAP-service-agent-only
            # invoker below (the identity gate), IAP is the sole access path.
            ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                # Scale to zero — idle cost ≈ 0 at this traffic level.
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0, max_instance_count=_MAX_INSTANCES
                ),
                # Direct VPC egress on the services network, all traffic, so the evidence and sheaf services
                # admit the BFF's calls at their run.app URLs (services_network.py).
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
                        # The Next.js server keeps a resident set above 500 MiB between requests.
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            limits={'cpu': '1', 'memory': '2Gi'},
                            # A set `resources` flips cpu_idle's default to false; nothing here runs
                            # between requests, so CPU stays request-billed.
                            cpu_idle=True,
                        ),
                        envs=[
                            _env('THEMIS_BACKEND', 'live'),
                            # Anthropic Managed-Agents client credentials: keyless WIF (Path B),
                            # no static API key (../../docs/runbooks/claude-api-wif.md).
                            _env('ANTHROPIC_ENVIRONMENT_ID', anthropic_environment_id),
                            _env('ANTHROPIC_FEDERATION_RULE_ID', anthropic_federation_rule_id),
                            _env('ANTHROPIC_ORGANIZATION_ID', anthropic_organization_id),
                            _env('ANTHROPIC_SERVICE_ACCOUNT_ID', anthropic_service_account_id),
                            _env('ANTHROPIC_WORKSPACE_ID', anthropic_workspace_id),
                            _env('THEMIS_ANTHROPIC_AGENT_ID', anthropic_agent_id),
                            _env('THEMIS_SESSION_TOKEN_KEY_VERSION', session_token_key_version),
                            _env('THEMIS_SQL_CONNECTION_NAME', sql_connection_name),
                            _env('THEMIS_SQL_DATABASE', sql_database),
                            _env('THEMIS_DB_USER', db_user.name),
                            _env('THEMIS_EVIDENCE_URL', evidence_url),
                            _env('THEMIS_FULLTEXT_BUCKET', fulltext_bucket),
                            _env('THEMIS_SHEAF_URL', sheaf_url),
                            _env('THEMIS_SHEAF_BUCKET', sheaf_bucket),
                            # The IAP-assertion audience the app verifies:
                            # /projects/<number>/locations/<region>/services/<name>.
                            _env('THEMIS_PROJECT_NUMBER', project_number),
                            _env('THEMIS_REGION', region),
                            _env('THEMIS_WEB_SERVICE_NAME', _SERVICE_NAME),
                            _env('THEMIS_TRACE_SAMPLE_RATIO', str(trace_sample_ratio)),
                        ],
                        # Probes reach the container directly, not through the LB, so they carry no
                        # IAP assertion; the app's proxy allowlists this path.
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            http_get=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeHttpGetArgs(
                                path='/api/healthz',
                            ),
                        ),
                    )
                ],
            ),
            opts=child,
        )

        # Identity gate: IAP invokes Cloud Run as its service agent (the
        # `X-Serverless-Authorization` flow), and only that agent may invoke — not allUsers.
        # With ingress locked to the LB (above), a request that reaches the
        # service must still authenticate as this agent, closing the
        # unauthenticated internal-VPC path that ingress alone leaves open.
        grants.ServiceInvoker(
            'iap-service-agent',
            member=iap_service_agent,
            service=self._service.name,
            project=project,
            location=region,
            target='web',
            prior=grants.Prior('themis-invoker', parent=self),
            opts=child,
        )

        self.service_name = self._service.name
        self.ip_address = self._build_load_balancer(
            'themis',
            project=project,
            region=region,
            domain=domain,
            routes=routes,
            child=child,
        )
        self.url = pulumi.Output.format('https://{0}', domain)
        self.register_outputs(
            {
                'ip_address': self.ip_address,
                'url': self.url,
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
                'db_user': self.db_user,
                'service_name': self.service_name,
            }
        )

    def _build_load_balancer(
        self,
        name: str,
        *,
        project: str,
        region: str,
        domain: str,
        routes: Sequence[Route | GrpcRoute],
        child: pulumi.ResourceOptions,
    ) -> pulumi.Output[str]:
        """Build the external HTTPS load balancer chain.

        Returns:
            The reserved global IP.
        """
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
            log_config=gcp.compute.BackendServiceLogConfigArgs(enable=True, sample_rate=1.0),
            opts=child,
        )

        certificate = gcp.compute.ManagedSslCertificate(
            f'{name}-cert',
            project=project,
            managed=gcp.compute.ManagedSslCertificateManagedArgs(domains=[domain]),
            opts=child,
        )
        check_routes(routes)
        url_map = gcp.compute.URLMap(
            f'{name}-urlmap',
            project=project,
            default_service=backend.id,
            # The load balancer serves one hostname, so the host rule matches any Host header; it exists
            # only because a path matcher is reached through one.
            host_rules=[gcp.compute.URLMapHostRuleArgs(hosts=['*'], path_matcher='routes')],
            path_matchers=[
                gcp.compute.URLMapPathMatcherArgs(
                    name='routes',
                    default_service=backend.id,
                    path_rules=[
                        gcp.compute.URLMapPathMatcherPathRuleArgs(
                            paths=[route.url_map_path], service=route.backend_service
                        )
                        for route in routes
                    ],
                )
            ],
            opts=child,
        )
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
