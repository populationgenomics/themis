"""The evidence service: one Cloud Run gRPC deployment for the data plane's evidence interfaces.

A runtime SA and an HTTP/2 gRPC service hosting every interface of the evidence image
(docs/design/services.md) — literature, plus the database-backed interfaces SVCv4 classification reaches
(variant, vep, gnomad, clinvar, gene_disease, transcript, splice, mavedb, cspec). Each is
configured by its own `THEMIS_<INTERFACE>_*` env vars, so a further one adds an env block and
whatever IAM it reads, not a service.

Literature (docs/design/document-pane.md) reads the litcache fulltext bucket (object-viewer,
read-only) to resolve a paper's rendering / PDF / associated-file object and locate a quote:
`THEMIS_LITERATURE_BACKEND=live`. It holds one Cloud SQL login, `SELECT` on `litcache.crosswalk` alone,
to resolve an external id to a `doc_id`; the table grant is the migration's, keyed on this login.

It is also the conversion lane's producer (docs/design/evidence-fulltext.md), so it carries the
`THEMIS_LITERATURE_CONVERT_*` trio and needs two grants the lane's own component cannot make: enqueue
on the queue, and `actAs` on the invoker service account. Both are the program's, beside the queue and
the invoker themselves.

Its caller is the web BFF — quote location for the document pane, plus some paper management. The BFF
has no Direct VPC egress (it reaches GCS / Cloud SQL / Anthropic over Google APIs, not the services
VPC), so an internal-ingress service would be unreachable from it. Ingress is therefore `ALL`, gated
by IAM: `run.invoker` is granted per caller in the program (`infra/__main__.py`) — the web SA, and the
`clu` automation user that reads the corpus by hand — and an unauthenticated request is refused (Cloud
Run default-deny, no other member). The web SA also holds object-viewer on the fulltext bucket — the
BFF serves the resolved object.

IAM is per-service, not per-rpc, so a `run.invoker` member reaches every interface on the deployment:
the web SA is granted invoke for the document pane's literature calls and reaches every other
interface with it. Splitting a caller off its unused rpcs would take a second deployment.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import sql


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


class EvidenceService(pulumi.ComponentResource):
    """Cloud Run evidence service (IAM-gated ingress) hosting the evidence image's interfaces.

    Attributes:
        service_account_email: The runtime SA's email — the `run.invoker` member on the auth service,
            and the reader of the buckets the interfaces use.
        db_user: The runtime SA's Cloud SQL IAM DB-user login — the crosswalk `SELECT` grant's
            subject, fed to the migrate runner as `EVIDENCE_DB_USER`.
        service_name: The Cloud Run service name, the subject of the program's invoker bindings.
        url: The service's ``run.app`` URL — the audience a caller's ID-token interceptor mints for.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        auth_url: pulumi.Input[str],
        vpc_network: pulumi.Input[str],
        vpc_subnetwork: pulumi.Input[str],
        fulltext_bucket: pulumi.Input[str],
        resources_bucket: pulumi.Input[str],
        sql_instance: gcp.sql.DatabaseInstance,
        sql_connection_name: pulumi.Input[str],
        sql_database: pulumi.Input[str],
        convert_queue_path: pulumi.Input[str],
        convert_worker_url: pulumi.Input[str],
        convert_invoker_sa_email: pulumi.Input[str],
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
        member = pulumi.Output.concat('serviceAccount:', service_account.email)

        # Read-only wherever the image only reads: literature resolves objects and locates quotes in
        # the litcache fulltext bucket (the cache warms via ingestion); gene_disease loads the four
        # reference dumps from the resources bucket at startup (the weekly refresh job holds the write
        # credential). Object-viewer is bucket-wide, so the second grant reaches every dataset in the
        # resources bucket, not the `gene-disease/` prefix alone.
        for label, bucket in (
            ('fulltext', fulltext_bucket),
            ('resources', resources_bucket),
        ):
            gcp.storage.BucketIAMMember(
                f'themis-evidence-{label}-object-viewer',
                bucket=bucket,
                role='roles/storage.objectViewer',
                member=member,
                opts=child,
            )

        # The crosswalk login and its connect roles. The table grant (SELECT only) is the
        # 0010_litcache_crosswalk_read_grant migration, keyed on this login.
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
            # token. The invoker bindings are not in this component — each caller gets `run.invoker`
            # in the program (`infra/__main__.py`); default-deny refuses any other.
            ingress='INGRESS_TRAFFIC_ALL',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0),
                # Direct VPC egress so the auth call arrives over the VPC and auth's internal ingress
                # admits it; all traffic, since the auth run.app and the public upstream hosts both
                # leave over the VPC (the services subnet's NAT provides the outbound path).
                vpc_access=gcp.cloudrunv2.ServiceTemplateVpcAccessArgs(
                    network_interfaces=[
                        gcp.cloudrunv2.ServiceTemplateVpcAccessNetworkInterfaceArgs(
                            network=vpc_network, subnetwork=vpc_subnetwork
                        )
                    ],
                    egress='ALL_TRAFFIC',
                ),
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=image,
                        # gene_disease holds the four parsed reference tables resident for the process's
                        # life: ~300 MiB of Python objects, plus the raw dumps alive alongside them
                        # while they parse.
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            limits={'cpu': '1', 'memory': '2Gi'},
                            # A set `resources` flips cpu_idle's default to false; nothing here runs
                            # between requests, so CPU stays request-billed.
                            cpu_idle=True,
                        ),
                        envs=[
                            # Image-wide: one session resolver over one auth service, for every
                            # interface that authorizes (literature does not).
                            _env('THEMIS_AUTHORIZER_BACKEND', 'http'),
                            _env('THEMIS_AUTH_URL', auth_url),
                            _env('THEMIS_LITERATURE_BACKEND', 'live'),
                            _env('THEMIS_FULLTEXT_BUCKET', fulltext_bucket),
                            # The crosswalk trio, all-or-nothing: the interface fails startup on a
                            # partial set rather than per request.
                            _env('THEMIS_LITERATURE_CROSSWALK_INSTANCE', sql_connection_name),
                            _env('THEMIS_LITERATURE_CROSSWALK_DATABASE', sql_database),
                            _env('THEMIS_LITERATURE_CROSSWALK_DB_USER', db_user.name),
                            # The conversion trio, all-or-nothing on the same terms.
                            _env('THEMIS_LITERATURE_CONVERT_QUEUE', convert_queue_path),
                            _env('THEMIS_LITERATURE_CONVERT_WORKER_URL', convert_worker_url),
                            _env('THEMIS_LITERATURE_CONVERT_INVOKER_SA', convert_invoker_sa_email),
                            _env('THEMIS_VARIANT_BACKEND', 'live'),
                            _env('THEMIS_VEP_BACKEND', 'live'),
                            _env('THEMIS_GNOMAD_BACKEND', 'live'),
                            _env('THEMIS_CLINVAR_BACKEND', 'live'),
                            _env('THEMIS_GENE_DISEASE_BACKEND', 'live'),
                            _env('THEMIS_RESOURCES_BUCKET', resources_bucket),
                            _env('THEMIS_TRANSCRIPT_BACKEND', 'live'),
                            _env('THEMIS_SPLICE_BACKEND', 'live'),
                            _env('THEMIS_MAVEDB_BACKEND', 'live'),
                            _env('THEMIS_CSPEC_BACKEND', 'live'),
                        ],
                        ports=gcp.cloudrunv2.ServiceTemplateContainerPortsArgs(name='h2c', container_port=8080),
                        # 120 s, against the defaults' 30 s: the port binds only after every interface
                        # has built its backend, and the reference downloads sit behind a Direct VPC
                        # egress path a cold instance takes a minute or more to establish.
                        startup_probe=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs(
                            grpc=gcp.cloudrunv2.ServiceTemplateContainerStartupProbeGrpcArgs(port=8080),
                            failure_threshold=12,
                            period_seconds=10,
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
