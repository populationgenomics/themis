"""Themis infrastructure entrypoint — one `pulumi up` per environment.

Reads the active stack's config and composes the per-concern modules. Every
environment runs this same program; all differences live in
`Pulumi.<stack>.yaml`. See README.md.
"""

from __future__ import annotations

import os

import pulumi

from themis_infra import backend, baseline, secrets, storage, web

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

# Per-run input, not committed config: CI sets the pushed ref; a first bring-up
# uses gcr.io/cloudrun/hello so the registry and edge come up before any build.
web_image = os.environ.get(_WEB_IMAGE_ENV)
if not web_image:
    raise RuntimeError(
        f'{_WEB_IMAGE_ENV} is not set. Set it to the web image to deploy. CI sets the '
        'pushed image ref; for a first bring-up use gcr.io/cloudrun/hello. See infra/README.md.'
    )

base = baseline.Baseline('themis', project=project, region=region)
site = web.WebService(
    'themis',
    project=project,
    region=region,
    domain=domain,
    image=web_image,
    iap_member=f'group:{iap_access_group}',
    opts=pulumi.ResourceOptions(depends_on=[base]),
)
orchestrator = backend.OrchestratorBackend('themis', project=project)
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

pulumi.export('image_registry', base.image_prefix)
pulumi.export('lb_ip', site.ip_address)
pulumi.export('url', site.url)
pulumi.export('backend_sa_email', orchestrator.service_account_email)
pulumi.export('backend_sa_unique_id', orchestrator.service_account_unique_id)
pulumi.export('fulltext_bucket', fulltext.name)
pulumi.export('fulltext_bucket_url', pulumi.Output.format('gs://{0}', fulltext.name))
pulumi.export('semantic_scholar_secret_id', semantic_scholar.secret_id)
