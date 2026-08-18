"""The evidence service: one Cloud Run gRPC deployment for the data plane's evidence interfaces.

A runtime SA and an HTTP/2 gRPC service hosting every interface of the evidence image
(docs/design/services.md), literature being the one it serves today. Each interface is configured by its
own `THEMIS_<INTERFACE>_*` env vars, so a further one adds an env block and whatever IAM it reads, not a
service.

Literature (docs/design/document-pane.md) reads the litcache fulltext bucket (object-viewer,
read-only) to resolve a paper's rendering / PDF / associated-file object and locate a quote:
`THEMIS_LITERATURE_BACKEND=live`. No auth service — the corpus is not session-scoped (entitlement is a
deferred non-goal). It does hold one Cloud SQL login, `SELECT` on `litcache.crosswalk` alone, to
resolve an external id to a `doc_id`; the table grant is the migration's, keyed on this login.

Its callers are the web BFF — quote location for the document pane, plus some paper management — and,
the primary consumer, the sandbox agents (reading a paper's full text and validating quotes at authoring
time). The BFF has no Direct VPC egress (it reaches GCS / Cloud SQL / Anthropic over Google APIs, not the
services VPC), so an internal-ingress service would be unreachable from it.
Ingress is therefore `ALL`, gated by IAM: only the web SA is granted `run.invoker` (audience = this
service's URL), and the web SA also holds object-viewer on the fulltext bucket — the BFF serves the
resolved object. Those grants and the BFF's `THEMIS_EVIDENCE_URL` are wired in the program
(`infra/__main__.py`); an unauthenticated request is refused (Cloud Run default-deny, no other member).
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import sql


class EvidenceService(pulumi.ComponentResource):
    """Cloud Run evidence service (IAM-gated ingress) hosting the evidence image's interfaces.

    Attributes:
        service_account_email: The runtime SA's email — the object-viewer on the fulltext bucket.
        db_user: The runtime SA's Cloud SQL IAM DB-user login — the crosswalk `SELECT` grant's
            subject, fed to the migrate runner as `EVIDENCE_DB_USER`.
        service_name: The Cloud Run service name, for the web SA's invoker binding.
        url: The service's ``run.app`` URL — the audience the BFF's ID-token interceptor mints for.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        fulltext_bucket: pulumi.Input[str],
        sql_instance: gcp.sql.DatabaseInstance,
        sql_connection_name: pulumi.Input[str],
        sql_database: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:EvidenceService', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-evidence-runtime',
            project=project,
            account_id='themis-evidence',
            display_name='Themis evidence service runtime',
            opts=child,
        )
        self.service_account_email = service_account.email

        # Read-only on the litcache fulltext bucket: the literature backend reads manifests +
        # renderings to resolve objects and locate quotes; it never writes (the cache warms via ingestion).
        gcp.storage.BucketIAMMember(
            'themis-evidence-fulltext-object-viewer',
            bucket=fulltext_bucket,
            role='roles/storage.objectViewer',
            member=pulumi.Output.concat('serviceAccount:', service_account.email),
            opts=child,
        )

        # The crosswalk login and its connect roles. The table grant (SELECT only) is the
        # 0008_litcache_crosswalk_read_grant migration, keyed on this login.
        db_user = sql.iam_db_user(
            'themis-evidence',
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            opts=child,
        )
        sql.grant_cloudsql_connect(
            'themis-evidence',
            project=project,
            service_account_email=service_account.email,
            opts=child,
        )
        self.db_user = db_user.name

        service = gcp.cloudrunv2.Service(
            'themis-evidence-service',
            project=project,
            name='themis-evidence',
            location=region,
            deletion_protection=False,
            # IAM-gated public ingress: the BFF (no VPC egress) reaches the run.app URL with an ID
            # token. The invoker binding is not in this component — the web SA's `run.invoker` is
            # granted in the program (`infra/__main__.py`); Cloud Run default-deny refuses any other.
            ingress='INGRESS_TRAFFIC_ALL',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        envs=[
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_LITERATURE_BACKEND', value='live'
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_LITERATURE_FULLTEXT_BUCKET', value=fulltext_bucket
                            ),
                            # The crosswalk trio, all-or-nothing: the interface fails startup on a
                            # partial set rather than answering UNAVAILABLE per request.
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_LITERATURE_CROSSWALK_INSTANCE', value=sql_connection_name
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_LITERATURE_CROSSWALK_DATABASE', value=sql_database
                            ),
                            gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                name='THEMIS_LITERATURE_CROSSWALK_DB_USER', value=db_user.name
                            ),
                        ],
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(name='h2c', container_port=8080),
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            grpc=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeGrpcArgs(port=8080),
                        ),
                    )
                ],
            ),
            opts=child,
        )
        self.service_name = service.name
        self.url = service.uri
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'db_user': self.db_user,
                'service_name': self.service_name,
                'url': self.url,
            }
        )
