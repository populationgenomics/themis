"""Cloud SQL (Postgres): the per-environment instance and the IAM DB-user helper.

IAM database authentication only. The runtime SAs' read/write split is enforced
by table GRANTs in the migrations, not here. `iam_db_user` (the login) and
`grant_cloudsql_connect` (the project roles to reach the instance) are called from
each service module, not this constructor — the instance can't depend on the
services' SAs (they need its connection name), so attaching from here would cycle.
See `docs/plans/managed-agents-wiring.md` §8.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Smallest shared-core tier; Cloud SQL has no scale-to-zero.
_TIER = 'db-f1-micro'
_DATABASE_VERSION = 'POSTGRES_16'
_DISK_SIZE_GB = 10
_RETAINED_DAILY_BACKUPS = 30
_PITR_LOG_RETENTION_DAYS = 7
_DATABASE_NAME = 'themis'
# Cloud SQL IAM SA login names are the SA email without this domain suffix
# (Postgres truncates the `.gserviceaccount.com` tail).
_IAM_SA_EMAIL_SUFFIX = '.gserviceaccount.com'

# Project-level roles each service SA needs over the connector: `client` opens the
# connection (Admin-API ephemeral cert), `instanceUser` authenticates as its IAM DB
# user. Neither has an instance-scoped IAM form — both granted at the project.
_CLOUD_SQL_CLIENT_ROLE = 'roles/cloudsql.client'
_CLOUD_SQL_INSTANCE_USER_ROLE = 'roles/cloudsql.instanceUser'


class CloudSqlDatabase(pulumi.ComponentResource):
    """A Postgres instance with IAM auth, daily backups, and 7-day PITR.

    Attributes:
        instance: The `DatabaseInstance`; passed to `iam_db_user`
            when a service creates its SA's DB login.
        instance_connection_name: The `project:region:instance` string the Cloud
            SQL connector dials (set as a container env var on each service).
        instance_name: The bare instance name.
        database: The application `Database` inside the instance.
        database_name: The application database's name.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:CloudSqlDatabase', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.instance = gcp.sql.DatabaseInstance(
            f'{name}-sql',
            project=project,
            # Explicit, stable name: referenced by the connector's connection
            # string and the console, not auto-generated.
            name=f'{name}-sql',
            region=region,
            database_version=_DATABASE_VERSION,
            # Provider-layer guard: refuse a destroy/replace that would drop the
            # data. A deliberate teardown edits this off first.
            deletion_protection=True,
            settings=gcp.sql.DatabaseInstanceSettingsArgs(
                # API-level guard: blocks an out-of-band `gcloud sql instances
                # delete`, which the provider-level flag above does not.
                deletion_protection_enabled=True,
                tier=_TIER,
                edition='ENTERPRISE',
                # Single zone, no HA — shared-core tiers can't do HA anyway.
                availability_type='ZONAL',
                disk_size=_DISK_SIZE_GB,
                disk_autoresize=True,
                # Enables IAM database authentication — the SA `sql.User`s below
                # log in through this; no password users.
                database_flags=[
                    gcp.sql.DatabaseInstanceSettingsDatabaseFlagArgs(
                        name='cloudsql.iam_authentication',
                        value='on',
                    ),
                ],
                backup_configuration=gcp.sql.DatabaseInstanceSettingsBackupConfigurationArgs(
                    enabled=True,
                    # PITR for Postgres is WAL archiving (not MySQL binary logs);
                    # `transaction_log_retention_days` sizes the recovery window.
                    point_in_time_recovery_enabled=True,
                    transaction_log_retention_days=_PITR_LOG_RETENTION_DAYS,
                    start_time='03:00',
                    backup_retention_settings=gcp.sql.DatabaseInstanceSettingsBackupConfigurationBackupRetentionSettingsArgs(
                        retained_backups=_RETAINED_DAILY_BACKUPS,
                        retention_unit='COUNT',
                    ),
                ),
                # Public IP but no authorized networks: an empty authorizedNetworks
                # list makes Cloud SQL's firewall refuse every direct connection.
                # The connector reaches the instance out-of-band via an Admin-API
                # ephemeral cert (IAM-gated mTLS), not an IP allowlist.
                ip_configuration=gcp.sql.DatabaseInstanceSettingsIpConfigurationArgs(
                    ipv4_enabled=True,
                ),
            ),
            # protect: refuse a Pulumi-layer destroy/replace (defense-in-depth with
            # the two deletion_protection flags above). ignore_changes:
            # disk_autoresize floats the disk above disk_size, so pinning it forces
            # a shrink-back diff every `up` — the path is the camelCase schema name,
            # not snake_case.
            opts=pulumi.ResourceOptions.merge(
                child, pulumi.ResourceOptions(protect=True, ignore_changes=['settings.diskSize'])
            ),
        )
        self.instance_connection_name = self.instance.connection_name
        self.instance_name = self.instance.name

        self.database = gcp.sql.Database(
            f'{name}-db',
            project=project,
            instance=self.instance.name,
            name=_DATABASE_NAME,
            # Delete-guard the database too: dropping/replacing it drops every table.
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True)),
        )
        self.database_name = self.database.name

        self.register_outputs(
            {
                'instance_connection_name': self.instance_connection_name,
                'instance_name': self.instance_name,
                'database_name': self.database_name,
            }
        )


def iam_db_user(
    name: str,
    *,
    project: str,
    instance: gcp.sql.DatabaseInstance,
    service_account_email: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.sql.User:
    """Create the Cloud SQL IAM database user a service account logs in as.

    A `sql.User(type=CLOUD_IAM_SERVICE_ACCOUNT)` is the Postgres role the GCP SA
    authenticates as (without it the SA has no DB principal). Its login name is the
    SA email with the `.gserviceaccount.com` suffix removed (the Postgres IAM SA
    convention). This does **not** grant the project roles a connection needs —
    call `grant_cloudsql_connect` for that; a login and the roles to reach it are
    separate concerns (a user can exist before, or be reused across, grants).
    Table privileges come from the migrations, not here.

    Called from the consuming service module so the user nests under that service;
    the instance can't depend on the services' SAs (they need its connection name),
    so the user attaches from the caller. Pass its `child` options as `opts`.

    Args:
        name: Resource-name prefix (the consuming service's stack name + role,
            e.g. `themis-web`).
        project: The GCP project holding the instance.
        instance: The Cloud SQL instance to create the login on.
        service_account_email: The SA's email; the DB user name is this with the
            `.gserviceaccount.com` suffix removed.
        opts: Resource options (parent/dependency wiring from the caller).

    Returns:
        The `sql.User` login for this SA.
    """
    return gcp.sql.User(
        f'{name}-sql-user',
        project=project,
        instance=instance.name,
        name=pulumi.Output.from_input(service_account_email).apply(
            lambda email: email.removesuffix(_IAM_SA_EMAIL_SUFFIX)
        ),
        type='CLOUD_IAM_SERVICE_ACCOUNT',
        opts=opts,
    )


def grant_cloudsql_connect(
    name: str,
    *,
    project: str,
    service_account_email: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Grant a service account the project roles to reach the instance.

    `cloudsql.client` opens the connection (Admin-API ephemeral cert),
    `cloudsql.instanceUser` authenticates as the IAM DB user. Neither has an
    instance-scoped IAM form, so both bind at the project. Independent of the DB
    user itself (`iam_db_user`): the same SA needs both, but the login and these
    connect roles are granted separately so either can vary — e.g. two SAs can
    reach the instance without both owning a DB user.

    Args:
        name: Resource-name prefix (the consuming service's stack name + role).
        project: The GCP project holding the IAM policy.
        service_account_email: The SA to grant; the member is `serviceAccount:<email>`.
        opts: Resource options (parent/dependency wiring from the caller).
    """
    member = pulumi.Output.concat('serviceAccount:', service_account_email)
    for role_slug, role in (
        ('cloudsql-client', _CLOUD_SQL_CLIENT_ROLE),
        ('cloudsql-instance-user', _CLOUD_SQL_INSTANCE_USER_ROLE),
    ):
        gcp.projects.IAMMember(
            f'{name}-{role_slug}',
            project=project,
            role=role,
            member=member,
            opts=opts,
        )
