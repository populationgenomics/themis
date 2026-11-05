"""The auth service: an HTTP/2 (gRPC) Cloud Run service over Cloud SQL and the ACL (docs/design/services.md).

Provisions the auth data-plane service for one environment — a runtime SA, an internal-ingress Cloud Run service,
and the SA's Cloud SQL IAM DB-user login. The container runs the `cloudsql` backend, reaching `session_context`
through the connector; that table's `SELECT` grant is applied by the migration (keyed on this login), not here. It
reads the latest version of the ACL secret `themis:aclSecret` names, which the site creates, writes and grants the
service's account read on (docs/design/access-control.md); the stack creates nothing for it. The account is
`runtime_account`'s, created outside the service so that a fresh environment's first, targeted `up` creates it before
the site's grant names it. Ingress is internal-only, so every caller (the web tier, and each data-plane service that
resolves sessions) attaches to the services VPC; each holds `run.invoker` on it, granted in the program entrypoint,
and the admission interceptor decides which rpcs each may call. The interceptor checks the audience of every
caller's ID token against the URL `cloud_run.service_url` builds, the one every caller is handed. The service writes
its spans to Cloud Trace at the stack's sample ratio, and each call's duration and status, and when each outcome of
loading the ACL last happened, as metrics; the operational alerts read them (`alerts.OperationalAlerts`).
"""

from __future__ import annotations

import re

import pulumi
import pulumi_gcp as gcp

from themis_infra import cloud_run, grants, sql

_SERVICE_NAME = 'themis-auth'
# A secret's resource name, its project by id or by number.
_SECRET_NAME = re.compile(r'projects/([a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]*)/secrets/[A-Za-z0-9_-]{1,255}')
# How often the service re-reads the ACL, and the longest one read may take (themis/services/auth/acl_holder.py
# and backend.py, which the program cannot import). A staleness limit at or under their sum lets a copy go stale
# before the next refresh replaces it.
ACL_REFRESH_INTERVAL_SECONDS = 60
ACL_READ_TIMEOUT_SECONDS = 30


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


def check_acl_secret(name: str, *, project: str, project_number: str) -> None:
    """Refuse an ACL secret that is not a secret in the deployment's own project, named by its resource name.

    Args:
        name: The secret's resource name, from `themis:aclSecret`.
        project: The deployment's project id.
        project_number: The deployment's project number; the name may give the project either way.

    Raises:
        ValueError: If `name` is not `projects/<project>/secrets/<id>`, or names another project.
    """
    match = _SECRET_NAME.fullmatch(name)
    if match is None:
        raise ValueError(
            f"the ACL secret must be a secret's resource name, projects/<project>/secrets/<id>, got {name!r}"
        )
    if match.group(1) not in (project, project_number):
        raise ValueError(f"the ACL secret must be in the deployment's project, {project}, got {name!r}")


def check_acl_staleness_limit(staleness_limit_seconds: int) -> None:
    """Refuse a staleness limit the service would refuse at start.

    Raises:
        ValueError: If the staleness limit does not exceed the service's refresh interval plus its read timeout.
    """
    floor = ACL_REFRESH_INTERVAL_SECONDS + ACL_READ_TIMEOUT_SECONDS
    if staleness_limit_seconds <= floor:
        raise ValueError(
            f'the ACL staleness limit must exceed the {ACL_REFRESH_INTERVAL_SECONDS} s refresh interval plus the '
            f'{ACL_READ_TIMEOUT_SECONDS} s read timeout ({floor} s), got {staleness_limit_seconds}'
        )


def runtime_account(*, project: str, opts: pulumi.ResourceOptions | None = None) -> gcp.serviceaccount.Account:
    """The auth service's runtime service account, `themis-auth`.

    Top-level, so a fresh environment's targeted `up` creates it before the site grants it read on the ACL secret.
    """
    return gcp.serviceaccount.Account(
        'themis-auth-runtime',
        project=project,
        account_id='themis-auth',
        display_name='Themis auth service runtime',
        opts=pulumi.ResourceOptions.merge(
            opts,
            # Aliased to its URN as `AuthService`'s child `themis-runtime`, so the account is kept, not replaced.
            pulumi.ResourceOptions(
                aliases=[
                    pulumi.Alias(name='themis-runtime', parent=pulumi.create_urn('themis', 'themis:infra:AuthService'))
                ]
            ),
        ),
    )


class AuthService(pulumi.ComponentResource):
    """Cloud Run auth service (internal ingress) over Cloud SQL and the site's ACL secret.

    Attributes:
        service_account_email: The runtime SA's email.
        db_user: The SA's Cloud SQL IAM DB-user login — `THEMIS_DB_USER` for
            the container, and the `${AUTH_DB_USER}` the `session_context` grant
            substitutes.
        service_name: The Cloud Run service's name (the invoker-binding target).
        url: The service's `run.app` URL, built from its name — the URL a caller dials and mints its ID token
            for, and the audience the service checks.
    """

    def __init__(
        self,
        *,
        project: str,
        project_number: str,
        region: str,
        image: pulumi.Input[str],
        sql_instance: gcp.sql.DatabaseInstance,
        sql_connection_name: pulumi.Input[str],
        sql_database: pulumi.Input[str],
        service_account: gcp.serviceaccount.Account,
        acl_secret: str,
        acl_staleness_limit_seconds: int,
        trace_sample_ratio: float,
        min_instances: int,
        max_instances: int,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Provision the runtime SA's DB login and the service.

        Args:
            project: The GCP project.
            project_number: The project's numeric id, part of the service's built URL.
            region: The service's region.
            image: The container image.
            sql_instance: The Cloud SQL instance `session_context` lives in.
            sql_connection_name: The instance's connection name, for the connector.
            sql_database: The application database.
            service_account: The runtime account (`runtime_account`).
            acl_secret: The resource name of the secret the site writes the ACL into and grants `service_account`
                read on.
            acl_staleness_limit_seconds: How long the service answers from its last good copy of the ACL while no
                new copy loads, before it refuses every request.
            trace_sample_ratio: The fraction of requests traced.
            min_instances: Instances kept warm while idle.
            max_instances: The instance cap; each instance pools its own Cloud SQL connections
                (themis/services/auth/cloudsql.py).
            opts: Resource options (dependency wiring).

        Raises:
            ValueError: See `check_acl_secret` and `check_acl_staleness_limit`.
        """
        check_acl_secret(acl_secret, project=project, project_number=project_number)
        check_acl_staleness_limit(acl_staleness_limit_seconds)
        super().__init__('themis:infra:AuthService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.service_account_email = service_account.email

        # Create the SA's Cloud SQL IAM DB-user login and grant it the connect roles; the
        # session_context SELECT grant is applied by the migration, keyed on this login.
        db_user = sql.iam_db_user(
            'themis-auth',
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            opts=child,
        )
        grants.DatabaseConnector(
            'themis-auth',
            member=grants.service_account(service_account.email),
            project=project,
            prior=grants.Prior('themis-auth', parent=self),
            opts=child,
        )
        self.db_user = db_user.name
        grants.ServiceTelemetry(
            'themis-auth', member=grants.service_account(service_account.email), project=project, opts=child
        )

        url = cloud_run.service_url(name=_SERVICE_NAME, project_number=project_number, region=region)
        service = gcp.cloudrunv2.Service(
            'themis-service',
            project=project,
            # Explicit, stable name (referenced by the deploy workflow and console).
            name=_SERVICE_NAME,
            location=region,
            deletion_protection=False,
            # Internal only: admits a request only if it arrives over a same-project VPC, so each caller
            # attaches to the services network (services_network.py) and holds `run.invoker`.
            ingress='INGRESS_TRAFFIC_INTERNAL_ONLY',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=min_instances, max_instance_count=max_instances
                ),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            # CPU stays allocated between requests: the ACL refresh is a background task,
                            # and with request-billed CPU it starves while the instance is idle.
                            cpu_idle=False,
                            startup_cpu_boost=True,
                        ),
                        envs=[
                            _env('THEMIS_BACKEND', 'cloudsql'),
                            _env('THEMIS_SQL_CONNECTION_NAME', sql_connection_name),
                            _env('THEMIS_SQL_DATABASE', sql_database),
                            _env('THEMIS_DB_USER', db_user.name),
                            _env('THEMIS_ACL_SECRET', acl_secret),
                            _env('THEMIS_ACL_STALENESS_LIMIT_SECONDS', str(acl_staleness_limit_seconds)),
                            _env('THEMIS_AUTHORIZER_BACKEND', 'http'),
                            _env('THEMIS_ID_TOKEN_AUDIENCE', url),
                            # completes a Caller's account id into the email the verifier yields
                            _env('THEMIS_GCP_PROJECT', project),
                            _env('THEMIS_TRACE_SAMPLE_RATIO', str(trace_sample_ratio)),
                        ],
                        # Serve gRPC: a named `h2c` port makes Cloud Run speak HTTP/2 cleartext
                        # to the container (TLS terminated at the ingress), and the startup probe
                        # checks the grpc.health.v1 service the server registers.
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(name='h2c', container_port=8080),
                        startup_probe=cloud_run.grpc_startup_probe(8080),
                    )
                ],
            ),
            opts=child,
        )
        self.service_name = service.name
        self.url = cloud_run.reported_url(service, url)
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'db_user': self.db_user,
                'service_name': self.service_name,
                'url': self.url,
            }
        )
