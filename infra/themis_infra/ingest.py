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

from themis_infra import grants, sql


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
        project_number: str,
        subnetwork: gcp.compute.Subnetwork,
        sql_instance: gcp.sql.DatabaseInstance,
        fulltext_bucket: pulumi.Input[str],
        secret_accessors: Mapping[str, pulumi.Input[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Provision the ingestion worker SA and its grants.

        Args:
            project: The GCP project to create the SA and project-level grant in.
            project_number: The project's numeric id, used to name the Dataflow
                service agent that needs `networkUser` on the workers' subnet.
            subnetwork: The ingestion workers' subnet; `networkUser` is granted
                on it to both principals that place workers there.
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

        grants.DataflowWorker(
            ingest_name,
            member=member,
            project=project,
            prior=grants.Prior(f'{ingest_name}-dataflow-worker', parent=self),
            opts=child,
        )

        # Both principals that place workers on the subnet: the worker SA and the Dataflow service
        # agent (which creates the worker VMs on the job's behalf).
        dataflow_agent = (
            f'serviceAccount:service-{project_number}@dataflow-service-producer-prod.iam.gserviceaccount.com'
        )
        for holder, label, principal in (
            (ingest_name, 'worker', member),
            ('dataflow-service-agent', 'agent', dataflow_agent),
        ):
            grants.SubnetUser(
                holder,
                member=principal,
                subnetwork=subnetwork.name,
                region=subnetwork.region,
                project=project,
                target='ingest-subnet',
                prior=grants.Prior(f'{ingest_name}-subnet-{label}', parent=self),
                opts=child,
            )
        # Seed sources in, the content-addressed cache out — both in the full-text bucket.
        grants.BucketObjectReadWriter(
            ingest_name,
            member=member,
            bucket=fulltext_bucket,
            role='roles/storage.objectUser',
            target='fulltext',
            prior=grants.Prior(f'{ingest_name}-fulltext', parent=self),
            opts=child,
        )
        for label, secret_id in secret_accessors.items():
            grants.SecretReader(
                ingest_name,
                member=member,
                secret=secret_id,
                project=project,
                target=label,
                prior=grants.Prior(f'{ingest_name}-secret-{label}', parent=self),
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
        grants.DatabaseConnector(
            ingest_name,
            member=member,
            project=project,
            prior=grants.Prior(ingest_name, parent=self),
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
