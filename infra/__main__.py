"""Themis infrastructure entrypoint — one `pulumi up` per environment.

Reads the active stack's config and composes the per-concern modules. Every
environment runs this same program; all differences live in
`Pulumi.<stack>.yaml`. See README.md.
"""

from __future__ import annotations

import os

import pulumi
import pulumi_gcp as gcp

from themis_infra import (
    auth,
    baseline,
    deploy_iam,
    hello,
    ingest,
    internal_lb,
    sandbox,
    secrets,
    sql,
    storage,
    store,
    web,
)

_WEB_IMAGE_ENV = 'THEMIS_WEB_IMAGE'
_AUTH_IMAGE_ENV = 'THEMIS_AUTH_IMAGE'
_STORE_IMAGE_ENV = 'THEMIS_STORE_IMAGE'
_HELLO_IMAGE_ENV = 'THEMIS_HELLO_IMAGE'

config = pulumi.Config()
gcp_config = pulumi.Config('gcp')

project = gcp_config.require('project')
region = gcp_config.require('region')
domain = config.require('domain')
iap_access_group = config.require('iapAccessGroup')
# Third-party ingestion key (no keyless/WIF path); the value is encrypted stack
# config. Provisioned into Secret Manager below; its runtime reader lands later.
semantic_scholar_api_key = config.require_secret('semanticScholarApiKey')
# Anthropic worker credential for the self-hosted sandbox; encrypted stack config.
# Provisioned into Secret Manager below; its reader (the dispatcher) lands with the
# sandbox compute slice.
anthropic_environment_key = config.require_secret('anthropicEnvironmentKey')


def _service_image(env_var: str, service_name: str) -> str:
    """The image to deploy for a Cloud Run service.

    An explicit override wins: `deploy.yml` sets the freshly-pushed ref, and a
    first bring-up passes `gcr.io/cloudrun/hello`. With no override — a PR
    `pulumi preview`, or a steady-state `up` — pin to the service's live image
    so the plan shows no spurious image change. Reading the live image requires
    the service to already exist, so a first bring-up must pass the override.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    live = gcp.cloudrunv2.get_service(name=service_name, location=region, project=project)
    return live.templates[0].containers[0].image


# The deploy SA's build-time roles (bootstrap keeps only the IAM/state/KMS root).
deploy_iam.grant_deploy_roles(project=project)

base = baseline.Baseline(project=project, region=region)
database = sql.CloudSqlDatabase(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
# The CI deploy SA as the migrations' owner — a Cloud SQL IAM DB user distinct
# from both runtime SAs. deploy.yml runs the migrations as it, so every table is
# owned by an identity neither runtime SA can impersonate; a table owner bypasses
# GRANTs, so the runtime SAs get only the table-level GRANTs the migrations apply.
migrator_email = deploy_iam.deploy_sa_email(project)
migrator_sql_user = sql.iam_db_user(
    'themis-migrator',
    project=project,
    instance=database.instance,
    service_account_email=migrator_email,
    # cloudsqlsuperuser gives the migrator CREATE on the public schema (a fresh IAM
    # user has none); the only password-free bootstrap, applied via the Admin API.
    database_roles=['cloudsqlsuperuser'],
    opts=pulumi.ResourceOptions(depends_on=[database]),
)
sql.grant_cloudsql_connect(
    'themis-migrator',
    project=project,
    service_account_email=migrator_email,
    opts=pulumi.ResourceOptions(depends_on=[database]),
)
auth_service = auth.AuthService(
    project=project,
    region=region,
    image=_service_image(_AUTH_IMAGE_ENV, 'themis-auth'),
    sql_instance=database.instance,
    sql_connection_name=database.instance_connection_name,
    sql_database=database.database_name,
    opts=pulumi.ResourceOptions(depends_on=[database]),
)
store_service = store.StoreService(
    project=project,
    region=region,
    image=_service_image(_STORE_IMAGE_ENV, 'themis-store'),
    auth_url=auth_service.url,
    custom_audiences=[internal_lb.audience(internal_lb.STORE_HOST)],
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
hello_service = hello.HelloService(
    project=project,
    region=region,
    image=_service_image(_HELLO_IMAGE_ENV, 'themis-hello'),
    auth_url=auth_service.url,
    custom_audiences=[internal_lb.audience(internal_lb.HELLO_HOST)],
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
# The store and hello services resolve session tokens through auth (§7); grant each SA invoke on the
# internal auth service — the binding auth left for when they landed.
for label, invoker_sa_email in (
    ('store', store_service.service_account_email),
    ('hello', hello_service.service_account_email),
):
    gcp.cloudrunv2.ServiceIamMember(
        f'themis-{label}-invokes-auth',
        project=project,
        location=region,
        name=auth_service.service_name,
        role='roles/run.invoker',
        member=pulumi.Output.concat('serviceAccount:', invoker_sa_email),
    )
site = web.WebService(
    project=project,
    region=region,
    domain=domain,
    image=_service_image(_WEB_IMAGE_ENV, 'themis-web'),
    iap_member=f'group:{iap_access_group}',
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
fulltext = storage.fulltext_bucket(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
semantic_scholar = secrets.semantic_scholar_secret(
    project=project,
    region=region,
    api_key=semantic_scholar_api_key,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
ingestion = ingest.IngestionRuntime(
    project=project,
    sql_instance=database.instance,
    fulltext_bucket=fulltext.name,
    secret_accessors={'semantic-scholar': semantic_scholar.secret_id},
    opts=pulumi.ResourceOptions(depends_on=[base, database, fulltext, semantic_scholar]),
)
# Self-hosted sandbox substrate (network + KMS + secret); the dispatcher and sandbox
# job that consume it land on the same module in the compute slice.
sandbox_network = sandbox.SandboxNetwork(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
# The internal load balancer that makes the store and hello services reachable from the locked-down
# sandbox at stable private hostnames on one IP (self-hosted-sandbox.md §8).
internal_services = internal_lb.InternalServiceLoadBalancer(
    project=project,
    region=region,
    network=sandbox_network.network.id,
    subnetwork=sandbox_network.subnetwork.id,
    response_policy=sandbox_network.response_policy.response_policy_name,
    services={
        internal_lb.STORE_HOST: store_service.service_name,
        internal_lb.HELLO_HOST: hello_service.service_name,
    },
    opts=pulumi.ResourceOptions(depends_on=[sandbox_network, store_service, hello_service]),
)
session_token_key = sandbox.session_token_signing_key(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
anthropic_environment_key_secret = sandbox.environment_key_secret(
    project=project,
    region=region,
    environment_key=anthropic_environment_key,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)

pulumi.export('image_registry', base.image_prefix)
pulumi.export('lb_ip', site.ip_address)
pulumi.export('url', site.url)
pulumi.export('web_sa_email', site.service_account_email)
pulumi.export('web_sa_unique_id', site.service_account_unique_id)
pulumi.export('sql_connection_name', database.instance_connection_name)
pulumi.export('sql_database', database.database_name)
# The deploy SA's DB login — the identity the deploy.yml migrate step authenticates
# as (the migrations' owner).
pulumi.export('migrator_sql_user', migrator_sql_user.name)
pulumi.export('auth_url', auth_service.url)
pulumi.export('auth_sa_email', auth_service.service_account_email)
pulumi.export('store_url', store_service.url)
pulumi.export('store_sa_email', store_service.service_account_email)
pulumi.export('store_working_document_bucket', store_service.working_document_bucket)
pulumi.export('store_workspace_bucket', store_service.workspace_bucket)
pulumi.export('hello_url', hello_service.url)
pulumi.export('hello_sa_email', hello_service.service_account_email)
pulumi.export('internal_lb_ip', internal_services.ip_address)
# The auth SA's DB login — the ${AUTH_DB_USER} the migrate step substitutes into the
# session_context SELECT grant.
pulumi.export('auth_sql_user', auth_service.sql_user)
pulumi.export('fulltext_bucket', fulltext.name)
pulumi.export('fulltext_bucket_url', pulumi.Output.format('gs://{0}', fulltext.name))
pulumi.export('semantic_scholar_secret_id', semantic_scholar.secret_id)
pulumi.export('ingest_sa_email', ingestion.service_account_email)
pulumi.export('ingest_sa_unique_id', ingestion.service_account_unique_id)
# The ingestion SA's DB login — the identity the Dataflow worker mints as.
pulumi.export('ingest_sql_user', ingestion.sql_user.name)
pulumi.export('sandbox_network', sandbox_network.network.id)
pulumi.export('sandbox_subnetwork', sandbox_network.subnetwork.id)
pulumi.export('sandbox_dns_response_policy', sandbox_network.response_policy.response_policy_name)
pulumi.export('session_token_signing_key', session_token_key.id)
pulumi.export('anthropic_environment_key_secret_id', anthropic_environment_key_secret.secret_id)
