"""Where an environment's logs are kept, and the audit logs it records (`docs/design/infrastructure.md`, Audit logging).

Every log entry the `_Default` sink routes lands in a log bucket in the stack's region, so case-bearing entries (the
upstream-call log, the database audit log) stay onshore. `_Required`, which holds the Admin Activity audit logs, stays
global: the platform fixes its location. Data Access audit logs record who read or changed a secret, a key, a database
or an object; the database's statements themselves come from pgAudit, which `sql.py` turns on and which writes through
Cloud SQL's Data Access audit log.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import baseline

LOG_BUCKET_ID = 'themis-logs'
# The services whose Data Access audit logs are on.
AUDITED_SERVICES = (
    'secretmanager.googleapis.com',
    'cloudkms.googleapis.com',
    'cloudsql.googleapis.com',
    'storage.googleapis.com',
)
# ADMIN_READ is included: a read of metadata or configuration, such as listing secrets or reading a key's policy, is
# part of who looked at what.
AUDIT_LOG_TYPES = ('ADMIN_READ', 'DATA_READ', 'DATA_WRITE')
# `_Default`'s own filter, which every project is created with: everything `_Required` does not take.
DEFAULT_SINK_FILTER = ' AND '.join(
    f'NOT LOG_ID("{log_id}")'
    for log_id in (
        'cloudaudit.googleapis.com/activity',
        'externalaudit.googleapis.com/activity',
        'cloudaudit.googleapis.com/system_event',
        'externalaudit.googleapis.com/system_event',
        'cloudaudit.googleapis.com/access_transparency',
        'externalaudit.googleapis.com/access_transparency',
    )
)


class Audit(pulumi.ComponentResource):
    """The regional log bucket, `_Default` routed into it, and Data Access audit logs on `AUDITED_SERVICES`.

    Attributes:
        log_bucket: The regional bucket every `_Default`-routed entry lands in.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:Audit', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # protect: deleting the bucket deletes every entry routed to it, the audit record included.
        self.log_bucket = gcp.logging.ProjectBucketConfig(
            'themis-logs',
            project=project,
            location=region,
            bucket_id=LOG_BUCKET_ID,
            retention_days=baseline.LOG_RETENTION_DAYS,
            description='Every entry the _Default sink routes, kept in the region.',
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True)),
        )

        # A sink named `_Default` adopts the project's existing one rather than creating a sink; deleting this
        # resource leaves the sink as last set.
        gcp.logging.ProjectSink(
            'themis-default-sink',
            project=project,
            name='_Default',
            destination=pulumi.Output.format(
                'logging.googleapis.com/projects/{0}/locations/{1}/buckets/{2}',
                project,
                self.log_bucket.location,
                self.log_bucket.bucket_id,
            ),
            filter=DEFAULT_SINK_FILTER,
            # The destination is in the sink's own project, so the platform's shared writer suffices.
            unique_writer_identity=False,
            opts=child,
        )

        for service in AUDITED_SERVICES:
            gcp.projects.IAMAuditConfig(
                f'themis-audit-{service.split(".", 1)[0]}',
                project=project,
                service=service,
                audit_log_configs=[
                    gcp.projects.IAMAuditConfigAuditLogConfigArgs(log_type=log_type) for log_type in AUDIT_LOG_TYPES
                ],
                opts=child,
            )

        self.register_outputs({'log_bucket': self.log_bucket.id})
