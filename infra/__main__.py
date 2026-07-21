"""Themis infrastructure entrypoint — one `pulumi up` per environment.

Reads the active stack's config and composes the per-concern modules. Every
environment runs this same program; all differences live in
`Pulumi.<stack>.yaml`. See README.md.
"""

from __future__ import annotations

import os
from collections.abc import Callable

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
    services_network,
    sql,
    storage,
    store,
    web,
)

_WEB_IMAGE_ENV = 'THEMIS_WEB_IMAGE'
_AUTH_IMAGE_ENV = 'THEMIS_AUTH_IMAGE'
_STORE_IMAGE_ENV = 'THEMIS_STORE_IMAGE'
_HELLO_IMAGE_ENV = 'THEMIS_HELLO_IMAGE'
_DISPATCHER_IMAGE_ENV = 'THEMIS_DISPATCHER_IMAGE'
_AGENT_IMAGE_ENV = 'THEMIS_AGENT_IMAGE'
_PROXY_IMAGE_ENV = 'THEMIS_PROXY_IMAGE'

# The sandbox forward leg's single consumer, and the working-document contract path.
_HELLO_METHOD = '/themis.rpc.hello.Hello/SayHello'
_WORKING_DOCUMENT_PATH = '/workspace/document.md'
# Sized comfortably above worst-case poll→ack (§5): the reclaim clock starts at the dispatcher's poll, so
# it must cover Job cold-start + Direct VPC egress cold-connect (§8, "a minute or more") + restore (up to
# the 180 s startup-probe window) — a booting item is then never reclaimed mid-restore and double-spawned.
# The cost of a wider window is that a genuinely failed spawn takes this long to become re-pollable. Task
# timeout is the longest legitimate session plus margin (§6). Tune from agent_run usage.
_RECLAIM_OLDER_THAN_MS = 600_000
_TASK_TIMEOUT_SECONDS = 3600

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
anthropic_environment_key = config.require_secret('anthropicEnvironmentKey')
# The webhook signing key (whsec_, encrypted config) the dispatcher verifies deliveries against, and the
# self-hosted environment id.
anthropic_webhook_signing_key = config.require_secret('anthropicWebhookSigningKey')
anthropic_environment_id = config.require('anthropicEnvironmentId')
anthropic_agent_id = config.require('anthropicAgentId')
# Anthropic Managed-Agents WIF (Path B) identifiers — plaintext, not credentials
# (docs/runbooks/claude-api-wif.md); the web app (the client) presents these.
anthropic_federation_rule_id = config.require('anthropicFederationRuleId')
anthropic_organization_id = config.require('anthropicOrganizationId')
anthropic_service_account_id = config.require('anthropicServiceAccountId')
anthropic_workspace_id = config.require('anthropicWorkspaceId')
# IAP-JWT audience inputs the web app verifies: the project's numeric id (a data-source
# lookup) and the backend service's numeric id. The backend fronts the web service, so its
# id can't be a live input to it (a cycle) — it is exported as web_backend_service_id and
# set here out of band after the first deploy, like the LB IP's A record.
project_number = gcp.organizations.get_project(project_id=project).number
iap_backend_service_id = config.require('iapBackendServiceId')


def _image(env_var: str, live: Callable[[], str]) -> str:
    """The image to deploy for a Cloud Run container (a service, or a job container).

    An explicit override wins: `deploy.yml` sets the freshly-pushed ref, and a
    first bring-up passes `gcr.io/cloudrun/hello`. With no override — a PR
    `pulumi preview`, or a steady-state `up` — read the resource's live image so
    the plan shows no spurious image change. Reading the live image requires the
    resource to already exist, so a first bring-up must pass the override.
    """
    return os.environ.get(env_var) or live()


def _live_service_image(service_name: str) -> str:
    service = gcp.cloudrunv2.get_service(name=service_name, location=region, project=project)
    return service.templates[0].containers[0].image


def _live_job_image(job_name: str, container_name: str) -> str:
    job = gcp.cloudrunv2.get_job(name=job_name, location=region, project=project)
    by_name = {container.name: container.image for container in job.templates[0].templates[0].containers}
    return by_name[container_name]


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
migrator_db_user = sql.iam_db_user(
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
    image=_image(_AUTH_IMAGE_ENV, lambda: _live_service_image('themis-auth')),
    sql_instance=database.instance,
    sql_connection_name=database.instance_connection_name,
    sql_database=database.database_name,
    opts=pulumi.ResourceOptions(depends_on=[database]),
)
# The internal services attach here (Direct VPC egress) to reach the internal-ingress auth service (§7).
services_net = services_network.ServicesNetwork(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
store_service = store.StoreService(
    project=project,
    region=region,
    image=_image(_STORE_IMAGE_ENV, lambda: _live_service_image('themis-store')),
    auth_url=auth_service.url,
    custom_audiences=[internal_lb.audience(internal_lb.STORE_HOST)],
    vpc_network=services_net.network.id,
    vpc_subnetwork=services_net.subnetwork.id,
    opts=pulumi.ResourceOptions(depends_on=[base, services_net]),
)
hello_service = hello.HelloService(
    project=project,
    region=region,
    image=_image(_HELLO_IMAGE_ENV, lambda: _live_service_image('themis-hello')),
    auth_url=auth_service.url,
    custom_audiences=[internal_lb.audience(internal_lb.HELLO_HOST)],
    vpc_network=services_net.network.id,
    vpc_subnetwork=services_net.subnetwork.id,
    opts=pulumi.ResourceOptions(depends_on=[base, services_net]),
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
# The KMS MAC key that signs per-session tokens (§7) — a shared substrate: the web BFF derives
# the bearer at session create, the dispatcher re-derives it at spawn.
session_token_key = sandbox.session_token_signing_key(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
session_token_key_version = pulumi.Output.concat(session_token_key.id, '/cryptoKeyVersions/1')
site = web.WebService(
    project=project,
    region=region,
    domain=domain,
    image=_image(_WEB_IMAGE_ENV, lambda: _live_service_image('themis-web')),
    iap_member=f'group:{iap_access_group}',
    sql_instance=database.instance,
    sql_connection_name=database.instance_connection_name,
    sql_database=database.database_name,
    session_token_key_version=session_token_key_version,
    working_document_bucket=store_service.working_document_bucket,
    anthropic_environment_id=anthropic_environment_id,
    anthropic_agent_id=anthropic_agent_id,
    anthropic_federation_rule_id=anthropic_federation_rule_id,
    anthropic_organization_id=anthropic_organization_id,
    anthropic_service_account_id=anthropic_service_account_id,
    anthropic_workspace_id=anthropic_workspace_id,
    project_number=project_number,
    iap_backend_service_id=iap_backend_service_id,
    opts=pulumi.ResourceOptions(depends_on=[base, database, store_service]),
)
# The web BFF signs session tokens with the MAC key and reads the working document from GCS.
gcp.kms.CryptoKeyIAMMember(
    'themis-web-mac-signer',
    crypto_key_id=session_token_key.id,
    role='roles/cloudkms.signerVerifier',
    member=pulumi.Output.concat('serviceAccount:', site.service_account_email),
)
gcp.storage.BucketIAMMember(
    'themis-web-working-document-viewer',
    bucket=store_service.working_document_bucket,
    role='roles/storage.objectViewer',
    member=pulumi.Output.concat('serviceAccount:', site.service_account_email),
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
# Self-hosted sandbox: the network + Anthropic secrets, then the internal load balancer, the sandbox
# job, and the dispatcher that run on it.
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
anthropic_environment_key_secret = sandbox.environment_key_secret(
    project=project,
    region=region,
    environment_key=anthropic_environment_key,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
anthropic_webhook_signing_key_secret = sandbox.webhook_signing_key_secret(
    project=project,
    region=region,
    signing_key=anthropic_webhook_signing_key,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
sandbox_job = sandbox.SandboxJob(
    project=project,
    region=region,
    agent_image=_image(_AGENT_IMAGE_ENV, lambda: _live_job_image('themis-sandbox', 'agent')),
    proxy_image=_image(_PROXY_IMAGE_ENV, lambda: _live_job_image('themis-sandbox', 'proxy')),
    network=sandbox_network.network.id,
    subnetwork=sandbox_network.subnetwork.id,
    # The proxy dials the services at their load-balancer hostnames and trusts the LB's self-signed cert.
    store_url=internal_lb.audience(internal_lb.STORE_HOST),
    hello_url=internal_lb.audience(internal_lb.HELLO_HOST),
    internal_ca_cert=internal_services.ca_cert_pem,
    forward_methods=_HELLO_METHOD,
    working_document_path=_WORKING_DOCUMENT_PATH,
    task_timeout_seconds=_TASK_TIMEOUT_SECONDS,
    opts=pulumi.ResourceOptions(depends_on=[base, sandbox_network, internal_services]),
)
# The job SA invokes only the sandbox-reachable services; inert without the proxy-held session token (§7).
for label, invoke_target in (('store', store_service.service_name), ('hello', hello_service.service_name)):
    gcp.cloudrunv2.ServiceIamMember(
        f'themis-sandbox-invokes-{label}',
        project=project,
        location=region,
        name=invoke_target,
        role='roles/run.invoker',
        member=pulumi.Output.concat('serviceAccount:', sandbox_job.service_account_email),
    )
dispatcher_service = sandbox.DispatcherService(
    project=project,
    region=region,
    image=_image(_DISPATCHER_IMAGE_ENV, lambda: _live_service_image('themis-dispatcher')),
    environment_id=anthropic_environment_id,
    environment_key_secret_id=anthropic_environment_key_secret.secret_id,
    webhook_signing_key_secret_id=anthropic_webhook_signing_key_secret.secret_id,
    session_token_key_id=session_token_key.id,
    sandbox_job_name=sandbox_job.job_name,
    reclaim_older_than_ms=_RECLAIM_OLDER_THAN_MS,
    opts=pulumi.ResourceOptions(depends_on=[base, sandbox_job]),
)
# The dispatcher runs the sandbox job with per-execution container overrides (the session env), a custom
# minimal role (§7). runWithOverrides is the override-carrying variant; run.jobs.run alone rejects them.
sandbox_job_runner_role = gcp.projects.IAMCustomRole(
    'themis-sandbox-job-runner',
    project=project,
    role_id='themisSandboxJobRunner',
    title='Themis sandbox job runner',
    permissions=['run.jobs.run', 'run.jobs.runWithOverrides'],
)
gcp.cloudrunv2.JobIamMember(
    'themis-dispatcher-runs-job',
    project=project,
    location=region,
    name=sandbox_job.job_name,
    role=sandbox_job_runner_role.name,
    member=pulumi.Output.concat('serviceAccount:', dispatcher_service.service_account_email),
)

pulumi.export('image_registry', base.image_prefix)
pulumi.export('lb_ip', site.ip_address)
pulumi.export('url', site.url)
pulumi.export('web_sa_email', site.service_account_email)
pulumi.export('web_sa_unique_id', site.service_account_unique_id)
# The web SA's DB login — the ${WEB_DB_USER} the migrate step substitutes into the
# analyses/session_context write grants.
pulumi.export('web_db_user', site.db_user)
# The IAP backend service's numeric id — set as themis:iapBackendServiceId after the first
# deploy so the web app can verify the IAP-JWT audience.
pulumi.export('web_backend_service_id', site.backend_service_id)
pulumi.export('sql_connection_name', database.instance_connection_name)
pulumi.export('sql_database', database.database_name)
# The deploy SA's DB login — the identity the deploy.yml migrate step authenticates
# as (the migrations' owner).
pulumi.export('migrator_db_user', migrator_db_user.name)
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
pulumi.export('auth_db_user', auth_service.db_user)
pulumi.export('fulltext_bucket', fulltext.name)
pulumi.export('fulltext_bucket_url', pulumi.Output.format('gs://{0}', fulltext.name))
pulumi.export('semantic_scholar_secret_id', semantic_scholar.secret_id)
pulumi.export('ingest_sa_email', ingestion.service_account_email)
pulumi.export('ingest_sa_unique_id', ingestion.service_account_unique_id)
# The ingestion SA's DB login — the identity the Dataflow worker mints as.
pulumi.export('ingest_db_user', ingestion.db_user.name)
pulumi.export('sandbox_network', sandbox_network.network.id)
pulumi.export('sandbox_subnetwork', sandbox_network.subnetwork.id)
pulumi.export('sandbox_dns_response_policy', sandbox_network.response_policy.response_policy_name)
pulumi.export('session_token_signing_key', session_token_key.id)
pulumi.export('anthropic_environment_key_secret_id', anthropic_environment_key_secret.secret_id)
pulumi.export('anthropic_webhook_signing_key_secret_id', anthropic_webhook_signing_key_secret.secret_id)
pulumi.export('sandbox_job_name', sandbox_job.job_name)
pulumi.export('sandbox_job_sa_email', sandbox_job.service_account_email)
pulumi.export('dispatcher_url', dispatcher_service.url)
pulumi.export('dispatcher_sa_email', dispatcher_service.service_account_email)
