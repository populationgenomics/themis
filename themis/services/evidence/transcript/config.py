"""The transcript interface's environment contract: `THEMIS_TRANSCRIPT_*` to a backend.

`THEMIS_TRANSCRIPT_BACKEND` selects the adapter (required — no silent default): `fixture` (in-memory,
seeded from `THEMIS_TRANSCRIPT_FIXTURE`) or `live` (VariantValidator, gnomAD, GTEx and NCBI ClinVar, over the image's
shared HTTP client).
"""

from __future__ import annotations

import os

from themis.services.evidence import deps as deps_mod
from themis.services.evidence.transcript import backend as transcript_backend

_BACKEND_VAR = 'THEMIS_TRANSCRIPT_BACKEND'
_FIXTURE_VAR = 'THEMIS_TRANSCRIPT_FIXTURE'


def backend_from_env(deps: deps_mod.Deps) -> transcript_backend.TranscriptBackend:
    """The adapter named by `THEMIS_TRANSCRIPT_BACKEND`, or `SystemExit`.

    Raises:
        SystemExit: the selector is unset or unknown, or the fixture's seed is missing or malformed.
    """
    backend = os.environ.get(_BACKEND_VAR)
    if backend is None:
        raise SystemExit(f'{_BACKEND_VAR} is required (expected "fixture" or "live")')
    if backend == 'fixture':
        return transcript_backend.fixture_backend_from_json(os.environ.get(_FIXTURE_VAR), var_name=_FIXTURE_VAR)
    if backend == 'live':
        return transcript_backend.LiveBackend(deps.http_client)
    raise SystemExit(f'unsupported {_BACKEND_VAR} {backend!r} (expected "fixture" or "live")')
