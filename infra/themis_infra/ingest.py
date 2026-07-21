"""The litcache ingestion runtime: a Dataflow worker identity + its grants.

The seed-ingestion pipeline (`litcache/ingest_beam.py`) runs on Dataflow. Like
the orchestrator backend, its runtime **service account** is forward-provisioned
here ahead of the job — the identity and its least-privilege grants exist so the
pipeline (in the litcache stack) can be launched against them. Kept a separate
identity from the backend and web by least privilege: only ingestion writes the
full-text store and reads the ingestion API keys.

Grants that wait on resources not yet in the program: a Dataflow staging/temp
bucket, and the launcher's `iam.serviceAccountUser` on this SA (the job launcher).
They attach when those land.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_gcp as gcp

from themis_infra import sql


class IngestionRuntime(pulumi.ComponentResource):
    """The litcache Dataflow ingestion worker's identity and data-plane grants.

    Attributes:
        service_account_email: The worker SA's email.
        service_account_unique_id: The worker SA's stable numeric id (never
            reused, so it survives a delete/recreate of the same email).
        db_user: The SA's Cloud SQL IAM DB-user login (the crosswalk mint).
    """

    def __init__(
        self,
        *,
        project: str,
        sql_instance: gcp.sql.DatabaseInstance,
        fulltext_bucket: pulumi.Input[str],
        secret_accessors: Mapping[str, pulumi.Input[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Provision the ingestion worker SA and its grants.

        Args:
            project: The GCP project to create the SA and project-level grant in.
            sql_instance: The Cloud SQL instance holding the crosswalk; the SA is
                attached as an IAM DB user on it (the mint login).
            fulltext_bucket: The full-text store bucket name; the SA gets
                object read/write on it (seed sources in, cache out).
            secret_accessors: Stable-label → Secret Manager `secret_id`; the SA
                gets `secretAccessor` on each (e.g. the Semantic Scholar key).
            opts: Resource options (dependency wiring).
        """
        super().__init__('themis:infra:IngestionRuntime', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)
        # The SA's resource name and account id, and the stem every grant nests
        # under — one value so the SA and its grants can't drift apart.
        ingest_name = 'themis-ingest'

        service_account = gcp.serviceaccount.Account(
            ingest_name,
            project=project,
            account_id=ingest_name,
            display_name='Themis litcache ingestion (Dataflow worker)',
            opts=child,
        )
        member = service_account.member

        # Run as a Dataflow worker. Project-scoped: the role has no resource form.
        gcp.projects.IAMMember(
            f'{ingest_name}-dataflow-worker',
            project=project,
            role='roles/dataflow.worker',
            member=member,
            opts=child,
        )
        # Read seed sources and write the content-addressed cache — both live in
        # the full-text bucket. objectUser, not objectAdmin: the bucket enforces
        # uniform access (no object ACLs to manage), and the writer is write-once.
        gcp.storage.BucketIAMMember(
            f'{ingest_name}-fulltext',
            bucket=fulltext_bucket,
            role='roles/storage.objectUser',
            member=member,
            opts=child,
        )
        # Read each ingestion API key at runtime, scoped to that one secret.
        for label, secret_id in secret_accessors.items():
            gcp.secretmanager.SecretIamMember(
                f'{ingest_name}-secret-{label}',
                project=project,
                secret_id=secret_id,
                role='roles/secretmanager.secretAccessor',
                member=member,
                opts=child,
            )

        # The crosswalk-mint login + the roles to reach the instance. Table-level
        # rights come from the migration (the migrator owns the `litcache` schema
        # and grants this SA SELECT/INSERT), never here.
        self.db_user = sql.iam_db_user(
            ingest_name,
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            opts=child,
        )
        sql.grant_cloudsql_connect(
            ingest_name,
            project=project,
            service_account_email=service_account.email,
            opts=child,
        )

        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
                'db_user': self.db_user.name,
            }
        )
