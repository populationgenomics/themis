"""The gene_disease interface's environment contract: `THEMIS_GENE_DISEASE_*` to a backend.

`THEMIS_GENE_DISEASE_BACKEND` selects the adapter (required — no silent default): `fixture`
(in-memory, seeded from `THEMIS_GENE_DISEASE_FIXTURE`) or `live` (the four reference dumps loaded
once from the shared resources bucket named by `THEMIS_RESOURCES_BUCKET`, with MONDO reached over
the image's shared HTTP client).

Building the live adapter reads the bucket, so it is the one build in the image that is `async`.
"""

from __future__ import annotations

import os

from themis.services.evidence import deps as deps_mod
from themis.services.evidence.gene_disease import backend as gene_disease_backend

_BACKEND_VAR = 'THEMIS_GENE_DISEASE_BACKEND'
_FIXTURE_VAR = 'THEMIS_GENE_DISEASE_FIXTURE'
_RESOURCES_BUCKET_VAR = 'THEMIS_RESOURCES_BUCKET'


async def backend_from_env(deps: deps_mod.Deps) -> gene_disease_backend.GeneDiseaseBackend:
    """The adapter named by `THEMIS_GENE_DISEASE_BACKEND`, or `SystemExit`.

    Raises:
        SystemExit: the selector is unset or unknown, the fixture's seed is missing or malformed, or
            the live adapter's resources bucket is unset.
    """
    backend = os.environ.get(_BACKEND_VAR)
    if backend is None:
        raise SystemExit(f'{_BACKEND_VAR} is required (expected "fixture" or "live")')
    if backend == 'fixture':
        return gene_disease_backend.fixture_backend_from_json(os.environ.get(_FIXTURE_VAR), var_name=_FIXTURE_VAR)
    if backend == 'live':
        resources_bucket = os.environ.get(_RESOURCES_BUCKET_VAR)
        if not resources_bucket:
            raise SystemExit(
                f'{_RESOURCES_BUCKET_VAR} is required for the live backend: the GCS bucket holding the '
                'gene-disease reference dumps'
            )
        return await gene_disease_backend.LiveBackend.create(
            http_client=deps.http_client, resources_bucket=resources_bucket
        )
    raise SystemExit(f'unsupported {_BACKEND_VAR} {backend!r} (expected "fixture" or "live")')
