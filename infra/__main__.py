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
    convert,
    deploy_iam,
    evidence,
    hello,
    ingest,
    ingest_network,
    sandbox,
    screenshots,
    secrets,
    services_network,
    sql,
    storage,
    store,
    web,
)

_CONVERT_WORKER_IMAGE_ENV = 'THEMIS_CONVERT_WORKER_IMAGE'
_WEB_IMAGE_ENV = 'THEMIS_WEB_IMAGE'
_AUTH_IMAGE_ENV = 'THEMIS_AUTH_IMAGE'
_STORE_IMAGE_ENV = 'THEMIS_STORE_IMAGE'
_HELLO_IMAGE_ENV = 'THEMIS_HELLO_IMAGE'
_DISPATCHER_IMAGE_ENV = 'THEMIS_DISPATCHER_IMAGE'
_SANDBOX_WORKER_IMAGE_ENV = 'THEMIS_SANDBOX_WORKER_IMAGE'
_EVIDENCE_IMAGE_ENV = 'THEMIS_EVIDENCE_IMAGE'
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
# Whether this environment hosts the public-read PR review screenshot bucket. Required, not
# defaulted: a stack must decide, so no environment inherits a public bucket by omission.
enable_pr_screenshot_bucket = config.require_bool('enablePrScreenshotBucket')
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
# lookup) and the backend service's numeric id — this stack's own web_backend_service_id
# output, fed back as config (docs/runbooks/fresh-environment.md §3).
project_number = gcp.organizations.get_project(project_id=project).number
iap_backend_service_id = config.require('iapBackendServiceId')
# OAuth client ids IAP accepts programmatically (docs/runbooks/iap-access.md).
iap_programmatic_clients: list[str] = config.require_object('iapProgrammaticClients')


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
    vpc_network=services_net.network.id,
    vpc_subnetwork=services_net.subnetwork.id,
    opts=pulumi.ResourceOptions(depends_on=[base, services_net]),
)
hello_service = hello.HelloService(
    project=project,
    region=region,
    image=_image(_HELLO_IMAGE_ENV, lambda: _live_service_image('themis-hello')),
    auth_url=auth_service.url,
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
# The litcache fulltext bucket + the evidence service that reads it — created before the web app so
# its URL can be passed to the BFF (THEMIS_EVIDENCE_URL).
fulltext = storage.fulltext_bucket(
    project=project,
    region=region,
    # The BFF 302s paper-content reads to signed URLs on this bucket, so the browser fetches its
    # bytes cross-origin from the workbench; allow that origin (pdf.js / fetch read the body).
    cors_origins=[f'https://{domain}'],
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
evidence_service = evidence.EvidenceService(
    project=project,
    region=region,
    image=_image(_EVIDENCE_IMAGE_ENV, lambda: _live_service_image('themis-evidence')),
    fulltext_bucket=fulltext.name,
    opts=pulumi.ResourceOptions(depends_on=[base, fulltext]),
)
# The on-demand conversion lane (architecture B): all fetch/convert work off the read service's
# request path. Nothing enqueues onto the queue yet — the reconcile sweep is its first producer.
convert_queue = convert.conversion_queue(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
convert_worker = convert.ConvertWorker(
    project=project,
    region=region,
    image=_image(_CONVERT_WORKER_IMAGE_ENV, lambda: _live_service_image('themis-convert-worker')),
    fulltext_bucket=fulltext.name,
    opts=pulumi.ResourceOptions(depends_on=[base, fulltext]),
)
convert_invoker = convert.ConversionInvoker(
    project=project,
    region=region,
    worker_service_name=convert_worker.service_name,
    opts=pulumi.ResourceOptions(depends_on=[convert_worker]),
)
site = web.WebService(
    project=project,
    region=region,
    domain=domain,
    image=_image(_WEB_IMAGE_ENV, lambda: _live_service_image('themis-web')),
    iap_member=f'group:{iap_access_group}',
    iap_programmatic_clients=iap_programmatic_clients,
    sql_instance=database.instance,
    sql_connection_name=database.instance_connection_name,
    sql_database=database.database_name,
    session_token_key_version=session_token_key_version,
    working_document_bucket=store_service.working_document_bucket,
    evidence_url=evidence_service.url,
    evidence_corpus_bucket=fulltext.name,
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
# The BFF resolves papers through the evidence service (its ID token, audience = the service URL)
# and serves the resolved object from the fulltext bucket itself — so grant the web SA invoke on
# evidence and read on the bucket.
gcp.cloudrunv2.ServiceIamMember(
    'themis-web-invokes-evidence',
    project=project,
    location=region,
    name=evidence_service.service_name,
    role='roles/run.invoker',
    member=pulumi.Output.concat('serviceAccount:', site.service_account_email),
)
gcp.storage.BucketIAMMember(
    'themis-web-fulltext-object-viewer',
    bucket=fulltext.name,
    role='roles/storage.objectViewer',
    member=pulumi.Output.concat('serviceAccount:', site.service_account_email),
)
semantic_scholar = secrets.semantic_scholar_secret(
    project=project,
    region=region,
    api_key=semantic_scholar_api_key,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
ingest_net = ingest_network.IngestionNetwork(
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
ingestion = ingest.IngestionRuntime(
    project=project,
    project_number=project_number,
    subnetwork=ingest_net.subnetwork,
    sql_instance=database.instance,
    fulltext_bucket=fulltext.name,
    secret_accessors={'semantic-scholar': semantic_scholar.secret_id},
    opts=pulumi.ResourceOptions(depends_on=[base, database, fulltext, semantic_scholar, ingest_net]),
)
# Self-hosted sandbox: the Anthropic secrets, then the sandbox job and the dispatcher that runs it
# (postern-sandbox-swap.md). No dedicated VPC / egress firewall / internal load balancer — the guest has
# zero network, and the trusted worker reaches the internal store over Direct VPC egress. (The
# session-token KMS key is the shared substrate created above.)
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
    worker_image=_image(_SANDBOX_WORKER_IMAGE_ENV, lambda: _live_job_image('themis-sandbox', 'worker')),
    network=services_net.network.id,
    subnetwork=services_net.subnetwork.id,
    store_url=store_service.url,
    hello_url=hello_service.url,
    task_timeout_seconds=_TASK_TIMEOUT_SECONDS,
    opts=pulumi.ResourceOptions(depends_on=[base, services_net, store_service, hello_service]),
)
# The job SA invokes the services the hatch forwards to (store + hello); inert without the worker-held
# session token (§7).
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

# Developer-workflow storage, unattached to the data plane: the review screenshots a
# rendered-surface PR ships with (docs/design/pr-screenshots.md).
if enable_pr_screenshot_bucket:
    pulumi.export(
        'pr_screenshot_bucket',
        screenshots.pr_screenshot_bucket(
            project=project,
            region=region,
            team_group=iap_access_group,
            opts=pulumi.ResourceOptions(depends_on=[base]),
        ).name,
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
# The auth SA's DB login — the ${AUTH_DB_USER} the migrate step substitutes into the
# session_context SELECT grant.
pulumi.export('auth_db_user', auth_service.db_user)
pulumi.export('fulltext_bucket', fulltext.name)
pulumi.export('fulltext_bucket_url', pulumi.Output.format('gs://{0}', fulltext.name))
pulumi.export('evidence_url', evidence_service.url)
pulumi.export('evidence_sa_email', evidence_service.service_account_email)
pulumi.export('semantic_scholar_secret_id', semantic_scholar.secret_id)
pulumi.export('ingest_sa_email', ingestion.service_account_email)
pulumi.export('ingest_sa_unique_id', ingestion.service_account_unique_id)
# The ingestion SA's DB login — the identity the Dataflow worker mints as.
pulumi.export('ingest_db_user', ingestion.db_user.name)
# The subnet the Dataflow launch targets (`--subnetwork`), consumed from outside this program.
pulumi.export('ingest_subnetwork', ingest_net.subnetwork.self_link)
pulumi.export('session_token_signing_key', session_token_key.id)
pulumi.export('anthropic_environment_key_secret_id', anthropic_environment_key_secret.secret_id)
pulumi.export('anthropic_webhook_signing_key_secret_id', anthropic_webhook_signing_key_secret.secret_id)
pulumi.export('sandbox_job_name', sandbox_job.job_name)
pulumi.export('sandbox_job_sa_email', sandbox_job.service_account_email)
pulumi.export('dispatcher_url', dispatcher_service.url)
pulumi.export('dispatcher_sa_email', dispatcher_service.service_account_email)
pulumi.export('convert_queue', convert_queue.name)
pulumi.export('convert_worker_url', convert_worker.url)
pulumi.export('convert_worker_sa_email', convert_worker.service_account_email)
