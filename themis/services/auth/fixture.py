"""The in-memory backend for offline runs, and the parser of its seeds.

Two seeds, both required, with ``{}`` the explicit empty value of the first:

- ``THEMIS_FIXTURE_BINDINGS``: a JSON object mapping each plaintext session token to its stored
  binding, e.g. ``{"tok-abc": {"analysis_id": "a1", "project_id": "p1"}}``. Tokens are hashed on load,
  so the backend holds only hashes, as the real store does.
- ``THEMIS_FIXTURE_ACL``: the ACL's JSON content, validated as any version of it is when the service
  loads it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import override

from themis.services.auth import backend as auth_backend

BINDINGS_VAR = 'THEMIS_FIXTURE_BINDINGS'
ACL_VAR = 'THEMIS_FIXTURE_ACL'

_BINDING_KEYS = frozenset({'analysis_id', 'project_id'})


class FixtureBackend(auth_backend.AuthBackend):
    """Bindings keyed by token hash, and one fixed version of the ACL.

    The invariant matches the real store: lookups are by ``hash_token`` of the bearer, never the plaintext.
    """

    def __init__(self, bindings_by_token_hash: Mapping[str, auth_backend.StoredSession], acl: bytes) -> None:
        self._by_hash = dict(bindings_by_token_hash)
        self._acl = acl

    @override
    async def resolve(self, session_token: str) -> auth_backend.StoredSession:
        try:
            return self._by_hash[auth_backend.hash_token(session_token)]
        except KeyError:
            raise auth_backend.UnresolvedError from None

    @override
    async def read_acl(self) -> bytes:
        return self._acl


def backend_from_env() -> FixtureBackend:
    """The fixture backend seeded from ``THEMIS_FIXTURE_BINDINGS`` and ``THEMIS_FIXTURE_ACL``, or ``SystemExit``."""
    raw = os.environ.get(BINDINGS_VAR)
    if raw is None:
        raise SystemExit(
            f'{BINDINGS_VAR} is required for the fixture backend: a JSON object of token -> binding, '
            'or "{}" for an explicit empty store'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'{BINDINGS_VAR} is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(f'{BINDINGS_VAR} must be a JSON object of token -> binding, got {type(seeds).__name__}')
    acl = os.environ.get(ACL_VAR)
    if acl is None:
        raise SystemExit(f"{ACL_VAR} is required for the fixture backend: the ACL's JSON content")
    bindings = {auth_backend.hash_token(token): _parse_binding(token, binding) for token, binding in seeds.items()}
    return FixtureBackend(bindings, acl.encode('utf-8'))


def _parse_binding(token: str, binding: object) -> auth_backend.StoredSession:
    digest = auth_backend.hash_token(token)[:8]
    where = f'{BINDINGS_VAR} binding (token sha256:{digest}…)'
    if not isinstance(binding, dict) or binding.keys() != _BINDING_KEYS:
        raise SystemExit(f'{where} must be a JSON object of exactly {sorted(_BINDING_KEYS)}')
    analysis_id, project_id = binding['analysis_id'], binding['project_id']
    if not isinstance(analysis_id, str) or not analysis_id or not isinstance(project_id, str) or not project_id:
        raise SystemExit(f'{where} must set analysis_id and project_id to non-empty strings')
    return auth_backend.StoredSession(analysis_id=analysis_id, project_id=project_id)
