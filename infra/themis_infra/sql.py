"""Cloud SQL (Postgres): the per-environment instance and the IAM DB-user helper.

IAM database authentication only. The runtime SAs' read/write split is enforced
by table GRANTs in the migrations, not here. `iam_db_user` (the login) and
`grants.DatabaseConnector` (the project roles to reach the instance) are called from
each service module, not this constructor — the instance can't depend on the
services' SAs (they need its connection name), so attaching from here would cycle.
See `docs/design/infrastructure.md`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

_DATABASE_VERSION = 'POSTGRES_16'
_DISK_SIZE_GB = 10
_RETAINED_DAILY_BACKUPS = 30
_PITR_LOG_RETENTION_DAYS = 7
_BACKUP_LOCATION = 'australia-southeast2'
_DATABASE_NAME = 'themis'
# Cloud SQL IAM SA login names are the SA email without this domain suffix
# (Postgres truncates the `.gserviceaccount.com` tail).
_IAM_SA_EMAIL_SUFFIX = '.gserviceaccount.com'
_AVAILABILITY_TYPES = frozenset({'ZONAL', 'REGIONAL'})
_WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MAINTENANCE_WINDOW = re.compile(r'(?P<day>\w{3}) (?P<hour>[01]\d|2[0-3]):00 UTC')
# The value that leaves the window unset, so Cloud SQL picks when to apply maintenance.
ANY_MAINTENANCE_WINDOW = 'any'


@dataclasses.dataclass(frozen=True)
class MaintenanceWindow:
    """The weekly hour in which Cloud SQL may restart the instance for maintenance."""

    day: int
    """Day of the week, 1 (Monday) to 7 (Sunday), as the Admin API numbers it."""
    hour: int
    """Hour of the day in UTC, 0 to 23."""


def parse_maintenance_window(value: str) -> MaintenanceWindow | None:
    """Parse a stack's maintenance window: `any`, or a weekday and an hour such as `Sun 16:00 UTC`.

    Returns:
        The window, or None for `any`.

    Raises:
        ValueError: If the value is neither.
    """
    if value == ANY_MAINTENANCE_WINDOW:
        return None
    match = _MAINTENANCE_WINDOW.fullmatch(value)
    if match is None or match['day'] not in _WEEKDAYS:
        raise ValueError(
            f"maintenance window must be '{ANY_MAINTENANCE_WINDOW}' or like 'Sun 16:00 UTC', got {value!r}"
        )
    return MaintenanceWindow(day=_WEEKDAYS.index(match['day']) + 1, hour=int(match['hour']))


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
        *,
        project: str,
        region: str,
        tier: str,
        availability_type: str,
        maintenance_window: MaintenanceWindow | None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Create the instance and its application database.

        Args:
            project: The GCP project.
            region: The instance's region.
            tier: The machine tier, such as `db-f1-micro` or `db-custom-1-3840`. A shared-core tier carries
                no SLA, whatever its availability.
            availability_type: `ZONAL`, or `REGIONAL` for a standby in a second zone.
            maintenance_window: When Cloud SQL may restart the instance for maintenance; None lets it choose.
            opts: Resource options.

        Raises:
            ValueError: If `availability_type` is neither `ZONAL` nor `REGIONAL`.
        """
        if availability_type not in _AVAILABILITY_TYPES:
            raise ValueError(
                f'availability_type must be one of {sorted(_AVAILABILITY_TYPES)}, got {availability_type!r}'
            )
        super().__init__('themis:infra:CloudSqlDatabase', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.instance = gcp.sql.DatabaseInstance(
            'themis-sql',
            project=project,
            # Explicit, stable name: referenced by the connector's connection
            # string and the console, not auto-generated.
            name='themis-sql',
            region=region,
            database_version=_DATABASE_VERSION,
            # Provider-layer guard: refuse a destroy/replace that would drop the
            # data. A deliberate teardown edits this off first.
            deletion_protection=True,
            settings=gcp.sql.DatabaseInstanceSettingsArgs(
                # API-level guard: blocks an out-of-band `gcloud sql instances
                # delete`, which the provider-level flag above does not.
                deletion_protection_enabled=True,
                tier=tier,
                edition='ENTERPRISE',
                availability_type=availability_type,
                maintenance_window=(
                    gcp.sql.DatabaseInstanceSettingsMaintenanceWindowArgs(
                        day=maintenance_window.day, hour=maintenance_window.hour
                    )
                    if maintenance_window is not None
                    else None
                ),
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
                    # Unset ⇒ Cloud SQL picks the enclosing `asia` multi-region,
                    # putting backups offshore.
                    location=_BACKUP_LOCATION,
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
                # Refuses any connection that is not a Cloud SQL connector's or the Auth Proxy's,
                # whatever network it comes from.
                connector_enforcement='REQUIRED',
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
            'themis-db',
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
    database_roles: Sequence[pulumi.Input[str]] | None = None,
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.sql.User:
    """Create the Cloud SQL IAM database user a service account logs in as.

    A `sql.User(type=CLOUD_IAM_SERVICE_ACCOUNT)` is the Postgres role the GCP SA
    authenticates as (without it the SA has no DB principal). Its login name is the
    SA email with the `.gserviceaccount.com` suffix removed (the Postgres IAM SA
    convention). This does **not** grant the project roles a connection needs —
    that is `grants.DatabaseConnector`; a login and the roles to reach it are
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
        database_roles: Postgres roles granted to the IAM DB user via the Admin API
            (e.g. `cloudsqlsuperuser` for the schema-owning migrator, which needs
            CREATE — a fresh IAM user has none). None for a plain login.
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
        database_roles=database_roles,
        opts=opts,
    )
