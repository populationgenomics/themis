"""The automation identity: the account a person or a script acts as to reach a deployed service.

The services are IAM-gated Cloud Run, which admits an ID token whose `aud` is the service's own URL.
A user credential cannot mint one — `gcloud auth print-identity-token --audiences=...` refuses
anything but a service account — so anything not already running as a service account impersonates
this one: a person at a terminal, a local script, a one-off job.

    gcloud auth print-identity-token \
      --impersonate-service-account=themis-clu@PROJECT.iam.gserviceaccount.com \
      --audiences=SERVICE_URL

The group holds `serviceAccountTokenCreator` on the account and nothing else; the account holds the
invoker bindings. That edge is the stable one: what a person can reach changes by editing this
account's grants, never by adding another binding that names people.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants, sql


class AutomationUser(pulumi.ComponentResource):
    """The impersonated identity, and the group permitted to impersonate it."""

    def __init__(
        self,
        *,
        project: str,
        group_member: str,
        sql_instance: gcp.sql.DatabaseInstance,
        migrator_db_role: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Create the account, grant the group impersonation, and give it a database login.

        Args:
            project: The GCP project id.
            group_member: The IAM member permitted to impersonate, `group:<email>`.
            sql_instance: The Cloud SQL instance to attach a login to.
            migrator_db_role: The migrator's DB-user login, granted to this one as a role.
            opts: Parent/provider options.
        """
        super().__init__('themis:infra:AutomationUser', 'themis-clu', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-clu',
            project=project,
            account_id='themis-clu',
            display_name='Themis automation user (impersonated to reach the deployed services)',
            opts=child,
        )
        self.service_account_email = service_account.email
        self.member = service_account.member

        # Impersonator, not user: minting an ID token for a service audience is `generateIdToken`, and
        # the impersonation is logged with its delegation chain, so a call is attributable to the person
        # behind it rather than to the account.
        grants.AccountImpersonator(
            'themis-clu-group',
            member=group_member,
            account=service_account.name,
            target='themis-clu',
            prior=grants.Prior('themis-clu-impersonation', parent=self),
            opts=child,
        )

        # The instance refuses direct connections, so reaching it means the connector and an IAM login.
        # Membership of the migrator role carries the rights: every table is owned by it, and an owner's
        # rights come with the role rather than with a GRANT. Applied here rather than by a migration
        # because a role cannot grant membership in itself, and the migrations run as the migrator —
        # the Admin API applies this as `cloudsqladmin`, which can.
        db_user = sql.iam_db_user(
            'themis-clu',
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            database_roles=[migrator_db_role],
            opts=child,
        )
        grants.DatabaseConnector(
            'themis-clu',
            member=self.member,
            project=project,
            prior=grants.Prior('themis-clu', parent=self),
            opts=child,
        )
        self.db_user = db_user.name
        self.register_outputs({})
