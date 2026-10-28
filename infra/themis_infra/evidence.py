"""The evidence service: one Cloud Run gRPC deployment for the data plane's evidence interfaces.

A runtime SA and an HTTP/2 gRPC service hosting every interface of the evidence image
(docs/design/services.md) — literature, plus the database-backed interfaces SVCv4 classification reaches
(variant, vep, gnomad, clinvar, gene_disease, transcript, splice, mavedb, cspec). Each is
configured by its own `THEMIS_<INTERFACE>_*` env vars, so a further one adds an env block and
whatever IAM it reads, not a service.

Literature (docs/design/document-pane.md) reads the litcache fulltext bucket to resolve a paper's
rendering / PDF / associated-file object and locate a quote: `THEMIS_LITERATURE_BACKEND=live`. It also
brings in a paper the corpus does not hold (docs/design/evidence-fulltext.md), its one write, so it holds
read-write on that bucket — the convert worker's role — and `INSERT` beside `SELECT` on
`litcache.crosswalk` for the claim that paper's ids make. The table grants are migrations', keyed on the
one Cloud SQL login below: there `INSERT` alone is what keeps every claim unaltered, where on the bucket
the same property is the code's.

It is also the conversion lane's producer (docs/design/evidence-fulltext.md), so it carries the
`THEMIS_LITERATURE_CONVERT_*` trio and needs two grants the lane's own component cannot make: enqueue
on the queue, and `actAs` on the invoker service account. Both are the program's, beside the queue and
the invoker themselves.

An ingest looks the paper up in OpenAlex with the stack's OpenAlex API key, which Cloud Run mounts from
Secret Manager as `THEMIS_LITERATURE_OPENALEX_API_KEY`; the runtime SA reads that one secret.

Its callers are the web BFF (quote location for the document pane, plus some paper management) and the
sandbox worker, both over the services VPC, and an operator from a laptop. Ingress therefore admits
internal traffic and the load balancer only: the VPC callers reach the `run.app` URL, and the operator
reaches the program's IAP-gated `operator_ingress.OperatorBackend` on the web load balancer. Each caller in
the project holds `run.invoker`, granted in the program (`infra/__main__.py`), as does IAP's service agent for
the operator, and Cloud Run refuses any other. The
service checks the audience of every caller's ID token itself, against the URL `cloud_run.service_url`
builds, because behind IAP Cloud Run admits IAP's service agent and not the caller. The web SA also
holds object-viewer on the fulltext bucket, because the BFF serves the resolved object.

IAM is per-service, not per-rpc, so a `run.invoker` member reaches every interface on the deployment:
the web SA is granted invoke for the document pane's literature calls and reaches every other
interface with it. Splitting a caller off its unused rpcs would take a second deployment.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import cloud_run, grants, secrets, sql

_SERVICE_NAME = 'themis-evidence'

# Every gRPC service the image serves, as its proto names it: the load balancer routes each one's methods to the
# operator backend. `infra/tests/test_operator_routes.py` holds the set to the image's interfaces.
GRPC_SERVICES = (
    'themis.rpc.clinvar.ClinVar',
    'themis.rpc.cspec.Cspec',
    'themis.rpc.gene_disease.GeneDisease',
    'themis.rpc.gnomad.Gnomad',
    'themis.rpc.literature.Literature',
    'themis.rpc.mavedb.MaveDb',
    'themis.rpc.splice.Splice',
    'themis.rpc.transcript.Transcript',
    'themis.rpc.variant.Variant',
    'themis.rpc.vep.Vep',
)

# Crosswalk reads open unpooled Cloud SQL connections, as many per instance as its worker threads, so
# the cap multiplies that against db-f1-micro's 25.
_MAX_INSTANCES = 2


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=name, value=value)


class EvidenceService(pulumi.ComponentResource):
    """Cloud Run evidence service (internal and load-balancer ingress) hosting the evidence image's interfaces.

    Attributes:
        service_account_email: The runtime SA's email — the `run.invoker` member on the auth service,
            and the reader of the buckets the interfaces use.
        db_user: The runtime SA's Cloud SQL IAM DB-user login — the subject of the crosswalk's table
            grants, `SELECT` and `INSERT`, fed to the migrate runner as `EVIDENCE_DB_USER`.
        service_name: The Cloud Run service name, the subject of the program's invoker bindings.
        url: The service's ``run.app`` URL — what a caller dials, and the audience its ID token names.
            It resolves once Cloud Run reports serving the service at it.
    """

    def __init__(
        self,
        *,
        project: str,
        project_number: str,
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
        openalex_api_key: secrets.StackSecret,
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

        # Read-only where the image only reads: gene_disease loads the four reference dumps from the
        # resources bucket at startup (the weekly refresh job holds the write credential).
        grants.BucketObjectReader(
            'themis-evidence',
            member=member,
            bucket=resources_bucket,
            target='resources',
            prior=grants.Prior('themis-evidence-resources-object-viewer', parent=self),
            opts=child,
        )

        # Read-write on the fulltext bucket, as the convert worker holds it (`convert.py`): literature
        # resolves objects and locates quotes there, and MaybeIngestPapers commits a manifest for a paper
        # the corpus does not hold. GCS IAM has no prefix scope, so this reaches the whole bucket; that
        # the ingest writes only into a `doc_id` it minted, or adopted with no manifest committed, is the
        # code's, not the grant's (docs/design/evidence-fulltext.md).
        grants.BucketObjectReadWriter(
            'themis-evidence',
            member=member,
            bucket=fulltext_bucket,
            role='roles/storage.objectUser',
            target='fulltext',
            opts=child,
        )

        # The crosswalk login and its connect roles. The table grants are migrations', keyed on this
        # login: 0010 the SELECT the lookup reads through, 0013 the INSERT the ingest's claim makes.
        db_user = sql.iam_db_user(
            'themis-evidence',
            project=project,
            instance=sql_instance,
            service_account_email=service_account.email,
            opts=child,
        )
        grants.DatabaseConnector(
            'themis-evidence',
            member=member,
            project=project,
            prior=grants.Prior('themis-evidence', parent=self),
            opts=child,
        )
        self.db_user = db_user.name

        url = cloud_run.service_url(name=_SERVICE_NAME, project_number=project_number, region=region)
        openalex_reader = grants.SecretReader(
            'themis-evidence',
            member=member,
            secret=openalex_api_key.secret.secret_id,
            project=project,
            target='openalex-api-key',
            opts=child,
        )

        service = gcp.cloudrunv2.Service(
            'themis-evidence-service',
            project=project,
            name=_SERVICE_NAME,
            location=region,
            deletion_protection=False,
            # Callers on the services VPC, and operators through the load balancer's IAP. The invoker
            # bindings are the program's (`infra/__main__.py`); default-deny refuses any other.
            ingress='INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER',
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=service_account.email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=0, max_instance_count=_MAX_INSTANCES
                ),
                # Direct VPC egress on the services network, all traffic: the public upstream hosts are
                # reached through the services subnet's NAT.
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
                            startup_cpu_boost=True,
                        ),
                        envs=[
                            # Image-wide: the auth interceptor gating every rpc resolves sessions over
                            # one auth service and completes a Caller's account id in this project
                            # (rpc-authorization.md).
                            _env('THEMIS_AUTHORIZER_BACKEND', 'http'),
                            _env('THEMIS_AUTH_URL', auth_url),
                            _env('THEMIS_GCP_PROJECT', project),
                            _env('THEMIS_ID_TOKEN_AUDIENCE', url),
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
                            secrets.secret_env('THEMIS_LITERATURE_OPENALEX_API_KEY', openalex_api_key.secret.secret_id),
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
                        # 120 s: the port binds only after every interface has built its backend, and
                        # the reference downloads sit behind a Direct VPC egress path a cold instance
                        # takes a minute or more to establish.
                        startup_probe=cloud_run.grpc_startup_probe(8080, budget_seconds=120),
                    )
                ],
            ),
            # Cloud Run resolves the mounted secret's `latest` when it deploys a revision: the version must
            # exist and the SA must already read it.
            opts=pulumi.ResourceOptions.merge(
                child, pulumi.ResourceOptions(depends_on=[openalex_api_key.version, openalex_reader])
            ),
        )
        self.service_name = service.name
        self.url = cloud_run.reported_url(service, url)
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'db_user': self.db_user,
                'service_name': self.service_name,
                'url': self.url,
            }
        )
