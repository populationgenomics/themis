"""Themis infrastructure entrypoint — one `pulumi up` per environment.

Reads the active stack's config and composes the per-concern modules. Every
environment runs this same program; all differences live in
`Pulumi.<stack>.yaml`. See README.md.
"""

from __future__ import annotations

import os

import pulumi
import pulumi_gcp as gcp

from themis_infra import baseline, deploy_iam, ingest, secrets, storage, web

_WEB_IMAGE_ENV = 'THEMIS_WEB_IMAGE'

config = pulumi.Config()
gcp_config = pulumi.Config('gcp')

project = gcp_config.require('project')
region = gcp_config.require('region')
domain = config.require('domain')
iap_access_group = config.require('iapAccessGroup')
# Third-party ingestion key (no keyless/WIF path); the value is encrypted stack
# config. Provisioned into Secret Manager below; its runtime reader lands later.
semantic_scholar_api_key = config.require_secret('semanticScholarApiKey')


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
deploy_iam.grant_deploy_roles('themis', project=project)

base = baseline.Baseline('themis', project=project, region=region)
site = web.WebService(
    'themis',
    project=project,
    region=region,
    domain=domain,
    image=_service_image(_WEB_IMAGE_ENV, 'themis-web'),
    iap_member=f'group:{iap_access_group}',
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
fulltext = storage.fulltext_bucket(
    'themis',
    project=project,
    region=region,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
semantic_scholar = secrets.semantic_scholar_secret(
    'themis',
    project=project,
    region=region,
    api_key=semantic_scholar_api_key,
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
ingestion = ingest.IngestionRuntime(
    'themis',
    project=project,
    fulltext_bucket=fulltext.name,
    secret_accessors={'semantic-scholar': semantic_scholar.secret_id},
    opts=pulumi.ResourceOptions(depends_on=[base, fulltext, semantic_scholar]),
)

pulumi.export('image_registry', base.image_prefix)
pulumi.export('lb_ip', site.ip_address)
pulumi.export('url', site.url)
pulumi.export('web_sa_email', site.service_account_email)
pulumi.export('web_sa_unique_id', site.service_account_unique_id)
pulumi.export('fulltext_bucket', fulltext.name)
pulumi.export('fulltext_bucket_url', pulumi.Output.format('gs://{0}', fulltext.name))
pulumi.export('semantic_scholar_secret_id', semantic_scholar.secret_id)
pulumi.export('ingest_sa_email', ingestion.service_account_email)
pulumi.export('ingest_sa_unique_id', ingestion.service_account_unique_id)
