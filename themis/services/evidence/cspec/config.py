"""The cspec interface's environment contract: `THEMIS_CSPEC_*` to a backend.

`THEMIS_CSPEC_BACKEND` selects the adapter (required — no silent default): `fixture` (in-memory,
seeded from `THEMIS_CSPEC_FIXTURE`) or `live` (the ClinGen CSpec Registry, over the image's shared HTTP client).
"""

from __future__ import annotations

import os

from themis.services.evidence import deps as deps_mod
from themis.services.evidence.cspec import backend as cspec_backend

_BACKEND_VAR = 'THEMIS_CSPEC_BACKEND'
_FIXTURE_VAR = 'THEMIS_CSPEC_FIXTURE'


def backend_from_env(deps: deps_mod.Deps) -> cspec_backend.CspecBackend:
    """The adapter named by `THEMIS_CSPEC_BACKEND`, or `SystemExit`.

    Raises:
        SystemExit: the selector is unset or unknown, or the fixture's seed is missing or malformed.
    """
    backend = os.environ.get(_BACKEND_VAR)
    if backend is None:
        raise SystemExit(f'{_BACKEND_VAR} is required (expected "fixture" or "live")')
    if backend == 'fixture':
        return cspec_backend.fixture_backend_from_json(os.environ.get(_FIXTURE_VAR), var_name=_FIXTURE_VAR)
    if backend == 'live':
        return cspec_backend.LiveBackend(deps.http_client)
    raise SystemExit(f'unsupported {_BACKEND_VAR} {backend!r} (expected "fixture" or "live")')
