"""The auth service: an internal, HTTP/2 (gRPC) Cloud Run service over Cloud SQL (docs/design/services.md).

Provisions the auth data-plane service for one environment — a runtime SA, an
internal-ingress Cloud Run service, and the SA's Cloud SQL IAM DB-user login. The
container runs the `cloudsql` backend, reaching `session_context` through the
connector; that table's `SELECT` grant is applied by the migration (keyed on this
login), not here. Ingress is internal-only with no invoker binding yet — the store
that calls it does not exist; its `run.invoker` attaches when it lands.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import sql


class AuthService(pulumi.ComponentResource):
    """Cloud Run auth service (internal ingress) over Cloud SQL.

    Attributes:
        service_account_email: The runtime SA's email.
        sql_user: The SA's Cloud SQL IAM DB-user login — `THEMIS_SQL_IAM_USER` for
            the container, and the `${AUTH_DB_USER}` the `session_context` grant
            substitutes.
        url: The service's `run.app` URL (internal ingress).
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        sql_instance: gcp.sql.DatabaseInstance,
        sql_connection_name: pulumi.Input[str],
        sql_database: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:AuthService', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            f'{name}-runtime',
            project=project,
            account_id=f'{name}-auth',
            display_name='Themis auth service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email

        # Create the SA's Cloud SQL IAM DB-user login and grant it the connect roles; the
        # session_context SELECT grant is applied by the migration, keyed on this login.
        db_user = sql.iam_db_user(
            f'{name}-auth',
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            opts=child,
        )
        sql.grant_cloudsql_connect(
            f'{name}-auth',
            project=project,
            service_account_email=service_account.email,
            opts=child,
        )
        self.sql_user = db_user.name

        service = gcp.cloudrunv2.Service(
            f'{name}-service',
            project=project,
            # Explicit, stable name (referenced by the deploy workflow and console).
            name=f'{name}-auth',
            location=region,
            # Internal only: reachable service-to-service (the store, later), never
            # from the public internet. Egress to the Cloud SQL Admin API is unaffected.
            ingress='INGRESS_TRAFFIC_INTERNAL_ONLY',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                # Scale to zero — idle cost ≈ 0 at the spike's traffic.
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name='THEMIS_BACKEND', value='cloudsql'),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_SQL_CONNECTION_NAME', value=sql_connection_name
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_SQL_DATABASE', value=sql_database
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_SQL_IAM_USER', value=db_user.name
                            ),
                        ],
                        # Serve gRPC: a named `h2c` port makes Cloud Run speak HTTP/2 cleartext
                        # to the container (TLS terminated at the ingress), and the startup probe
                        # checks the grpc.health.v1 service the server registers.
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(name='h2c', container_port=8080),
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            grpc=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeGrpcArgs(port=8080),
                        ),
                    )
                ],
            ),
            opts=child,
        )
        self.url = service.uri
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'sql_user': self.sql_user,
                'url': self.url,
            }
        )
