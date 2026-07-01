"""The litcache ingestion runtime: a Dataflow worker identity + its grants.

The seed-ingestion pipeline (`litcache/ingest_beam.py`) runs on Dataflow. Like
the orchestrator backend, its runtime **service account** is forward-provisioned
here ahead of the job — the identity and its least-privilege grants exist so the
pipeline (in the litcache stack) can be launched against them. Kept a separate
identity from the backend and web by least privilege: only ingestion writes the
full-text store and reads the ingestion API keys.

Grants that wait on resources not yet in the program: the Cloud SQL `client`
grant (the crosswalk instance), a Dataflow staging/temp bucket, and the launcher's
`iam.serviceAccountUser` on this SA (the job launcher). They attach when those land.
"""

from __future__ import annotations

from collections.abc import Mapping

import pulumi
import pulumi_gcp as gcp


class IngestionRuntime(pulumi.ComponentResource):
    """The litcache Dataflow ingestion worker's identity and data-plane grants.

    Attributes:
        service_account_email: The worker SA's email.
        service_account_unique_id: The worker SA's stable numeric id (never
            reused, so it survives a delete/recreate of the same email).
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        fulltext_bucket: pulumi.Input[str],
        secret_accessors: Mapping[str, pulumi.Input[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Provision the ingestion worker SA and its grants.

        Args:
            name: Resource-name prefix (the stack name).
            project: The GCP project to create the SA and project-level grant in.
            fulltext_bucket: The full-text store bucket name; the SA gets
                object read/write on it (seed sources in, cache out).
            secret_accessors: Stable-label → Secret Manager `secret_id`; the SA
                gets `secretAccessor` on each (e.g. the Semantic Scholar key).
            opts: Resource options (dependency wiring).
        """
        super().__init__('themis:infra:IngestionRuntime', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            f'{name}-ingest',
            project=project,
            account_id=f'{name}-ingest',
            display_name='Themis litcache ingestion (Dataflow worker)',
            opts=child,
        )
        member = service_account.member

        # Run as a Dataflow worker. Project-scoped: the role has no resource form.
        gcp.projects.IAMMember(
            f'{name}-ingest-dataflow-worker',
            project=project,
            role='roles/dataflow.worker',
            member=member,
            opts=child,
        )
        # Read seed sources and write the content-addressed cache — both live in
        # the full-text bucket. objectUser, not objectAdmin: the bucket enforces
        # uniform access (no object ACLs to manage), and the writer is write-once.
        gcp.storage.BucketIAMMember(
            f'{name}-ingest-fulltext',
            bucket=fulltext_bucket,
            role='roles/storage.objectUser',
            member=member,
            opts=child,
        )
        # Read each ingestion API key at runtime, scoped to that one secret.
        for label, secret_id in secret_accessors.items():
            gcp.secretmanager.SecretIamMember(
                f'{name}-ingest-secret-{label}',
                project=project,
                secret_id=secret_id,
                role='roles/secretmanager.secretAccessor',
                member=member,
                opts=child,
            )

        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
            }
        )
