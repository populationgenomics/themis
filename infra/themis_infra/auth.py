"""The auth service: an HTTP/2 (gRPC) Cloud Run service over Cloud SQL and the ACL (docs/design/services.md).

Provisions the auth data-plane service for one environment — a runtime SA, an internal-ingress Cloud Run service,
and the SA's Cloud SQL IAM DB-user login. The container runs the `cloudsql` backend, reaching `session_context`
through the connector; that table's `SELECT` grant is applied by the migration (keyed on this login), not here. It
reads the latest version of the ACL secret, which `AclSecret` creates in the stack's project and the site's writer
fills (docs/design/access-control.md). Besides that writer and the deploy account, which administers every secret in
the stack, the service's account is the only one that may read it. The account is `runtime_account`'s, created
outside the service so that a fresh environment's first, targeted `up` grants it read long before the service's
first revision starts. Ingress is internal-only, so every caller (the web tier, and each data-plane service that
resolves sessions) attaches to the services VPC; each holds `run.invoker` on it, granted in the program entrypoint,
and the admission interceptor decides which rpcs each may call. The interceptor checks the audience of every
caller's ID token against the URL `cloud_run.service_url` builds, the one every caller is handed. The service writes
its spans to Cloud Trace at the stack's sample ratio.

`acl_rejection_alert` pages when the service refuses a version of the ACL.
"""

from __future__ import annotations

import re

import pulumi
import pulumi_gcp as gcp

from themis_infra import cloud_run, grants, secrets, sql

# Each instance pools up to 4 Cloud SQL connections, so 3 hold at most 12 of db-f1-micro's 25.
_MAX_INSTANCES = 3

_SERVICE_NAME = 'themis-auth'
# The secret the site writes the ACL into, in the stack's own project.
ACL_SECRET_ID = 'acl'  # noqa: S105 — the secret's name, not its value
# What the service's log line starts with, at ERROR, when a version of the ACL is unreadable or breaks a rule
# (themis/services/auth/acl_holder.py, which the program cannot import).
ACL_REJECTED = 'acl rejected:'
# A service account's email: the site's writer is automation, named as the stack names every principal, by address.
_SERVICE_ACCOUNT_EMAIL = re.compile(r'[a-z0-9][a-z0-9-]*@[a-z0-9][a-z0-9.-]*\.gserviceaccount\.com')
# How often the service re-reads the ACL, and the longest one read may take (themis/services/auth/acl_holder.py
# and backend.py, which the program cannot import). A staleness limit at or under their sum lets a copy go stale
# before the next refresh replaces it.
ACL_REFRESH_INTERVAL_SECONDS = 60
ACL_READ_TIMEOUT_SECONDS = 30


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


def check_acl_writer(writer: str) -> None:
    """Refuse an ACL writer that is not a service account's email.

    Raises:
        ValueError: If `writer` is not `<name>@<domain>.gserviceaccount.com`.
    """
    if not _SERVICE_ACCOUNT_EMAIL.fullmatch(writer):
        raise ValueError(
            f"the ACL writer must be a service account's email, <name>@<domain>.gserviceaccount.com, got {writer!r}"
        )


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

    Top-level, so a fresh environment's targeted `up` creates it with `AclSecret`, ahead of the service.
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


class AclSecret(pulumi.ComponentResource):
    """The secret the site writes the ACL into, empty until its writer adds a version, and the grants on it.

    These grants let the writer manage the secret's versions and read them, and reach no other secret: it reads back
    only what it writes. It may also bill API calls to the project (`grants.QuotaConsumer`).

    A component of its own, so a fresh environment can deploy it alone (`docs/runbooks/fresh-environment.md`): the
    auth service refuses to start until the site has added a valid version, and its read grant has to apply first.
    The secret is protected, since re-creating it empties it until the site writes again.

    Attributes:
        secret_id: The secret's id, `ACL_SECRET_ID`.
        name: The secret's resource name, `projects/<project>/secrets/<id>`, the one the site writes to.
        reader_grant: The auth service's read grant, which its service waits on.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        writer: str,
        reader: gcp.serviceaccount.Account,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Provision the secret, the writer's grants to manage and read its versions, and the reader's grant.

        Args:
            project: The GCP project, the deployment's own.
            region: The single region the secret replicates to.
            writer: The email of the site's service account that adds the ACL's versions.
            reader: The auth service's runtime account (`runtime_account`).
            opts: Resource options (dependency wiring).

        Raises:
            ValueError: See `check_acl_writer`.
        """
        check_acl_writer(writer)
        super().__init__('themis:infra:AclSecret', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)
        secret = secrets.site_written_secret(ACL_SECRET_ID, project=project, region=region, opts=child)
        writer_member = grants.service_account(writer)
        grants.SecretVersionManager(
            'site-acl-writer',
            member=writer_member,
            secret=secret.secret_id,
            project=project,
            target=ACL_SECRET_ID,
            opts=child,
        )
        grants.SecretReader(
            'site-acl-writer',
            member=writer_member,
            secret=secret.secret_id,
            project=project,
            target=ACL_SECRET_ID,
            opts=child,
        )
        # A writer whose client bills each call to the resource's project (`user_project_override`) needs
        # `serviceusage.services.use` here to read back a version it added.
        grants.QuotaConsumer('site-acl-writer', member=writer_member, project=project, opts=child)
        self.reader_grant = grants.SecretReader(
            'themis-auth',
            member=grants.service_account(reader.email),
            secret=secret.secret_id,
            project=project,
            target=ACL_SECRET_ID,
            opts=child,
        )
        self.secret_id = secret.secret_id
        self.name = pulumi.Output.concat('projects/', project, '/secrets/', secret.secret_id)
        self.register_outputs({'secret_id': self.secret_id, 'name': self.name})


class AuthService(pulumi.ComponentResource):
    """Cloud Run auth service (internal ingress) over Cloud SQL and the ACL secret.

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
        acl_secret: AclSecret,
        acl_staleness_limit_seconds: int,
        trace_sample_ratio: float,
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
            acl_secret: The secret the site writes the ACL into, which grants `service_account` read.
            acl_staleness_limit_seconds: How long the service answers from its last good copy of the ACL while no
                new copy loads, before it refuses every request.
            trace_sample_ratio: The fraction of requests traced.
            opts: Resource options (dependency wiring).

        Raises:
            ValueError: See `check_acl_staleness_limit`.
        """
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
        grants.TraceWriter(
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
                # Scale to zero — idle cost ≈ 0 at the spike's traffic. The cap bounds the
                # connections the auth pool can hold (themis/services/auth/cloudsql.py).
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0, max_instance_count=_MAX_INSTANCES
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
                            _env('THEMIS_ACL_SECRET', acl_secret.name),
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
            # A starting instance reads the ACL before it serves, so the read grant has to be in place first.
            opts=pulumi.ResourceOptions(parent=self, depends_on=[acl_secret.reader_grant]),
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


def acl_rejection_alert(
    *,
    project: str,
    notification_channel: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.monitoring.AlertPolicy:
    """Page when the auth service refuses a version of the ACL: the site's writer added one it cannot use.

    A log-match condition on the service's own rejection line, which names the rule the version broke. The
    service keeps answering from its last good copy until the staleness limit, so the alert is the warning
    that a refusal of everyone is coming.

    Args:
        project: The GCP project the service logs to.
        notification_channel: The Monitoring notification channel the incident posts to.
        opts: Resource options (dependency wiring).
    """
    log_filter = (
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{_SERVICE_NAME}" AND "{ACL_REJECTED}"'
    )
    return gcp.monitoring.AlertPolicy(
        'themis-auth-acl-rejected',
        project=project,
        display_name='Themis auth: ACL rejected',
        combiner='OR',
        severity='ERROR',
        conditions=[
            gcp.monitoring.AlertPolicyConditionArgs(
                display_name='The auth service rejected the ACL',
                condition_matched_log=gcp.monitoring.AlertPolicyConditionConditionMatchedLogArgs(filter=log_filter),
            )
        ],
        notification_channels=[notification_channel],
        # A log-match condition needs a rate limit: a bad version is logged again at every refresh, once a minute.
        alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(
            notification_rate_limit=gcp.monitoring.AlertPolicyAlertStrategyNotificationRateLimitArgs(period='3600s'),
            auto_close='86400s',
        ),
        documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
            mime_type='text/markdown',
            subject='Themis: the auth service rejected the ACL',
            content=(
                f'The `{_SERVICE_NAME}` service could not use the latest version of the ACL: it was unreadable, or it '
                f'broke one of the rules the service checks. The log line, which starts `{ACL_REJECTED}`, names the '
                f'cause. The service answers from its last good copy until the staleness limit '
                f'(`themis:aclStalenessLimitSeconds`), then refuses everyone until a valid version loads. Its logs: '
                f"`gcloud logging read '{log_filter}' --project {project} --limit 5`. "
                f'Runbook: `docs/runbooks/access-control.md`.'
            ),
        ),
        opts=opts,
    )
