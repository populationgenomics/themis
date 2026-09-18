"""What the evidence image builds once and every interface that needs it is handed.

Two things are the image's rather than any one interface's. Authorization is the same wherever it
applies — one interceptor gating every rpc of every interface, resolving the same session token
through the same auth service and every caller's ID token against the same certificates — so an image-wide
`THEMIS_AUTHORIZER_BACKEND` selects it, not a per-interface copy of the same value: a resolver each
would hold ten idle gRPC channels to auth in place of one. And the nine database-backed interfaces
reach public HTTP upstreams, as does `literature`'s discovery half, so they share one
`httpx2.AsyncClient`: a client each would be ten connection pools against overlapping hosts.

Everything else stays the interface's own — which adapter its port builds, and the vars that
configure it (`services.md`, "One deployment, several interfaces").
"""

from __future__ import annotations

import contextlib
import dataclasses
import os

import httpx2

from themis.clients.auth import caller as caller_mod
from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import session as session_mod
from themis.services.evidence.upstreams import destinations

_AUTHORIZER_VAR = 'THEMIS_AUTHORIZER_BACKEND'
_FIXTURE_CONTEXTS_VAR = 'THEMIS_EVIDENCE_FIXTURE_CONTEXTS'
_FIXTURE_CALLERS_VAR = 'THEMIS_EVIDENCE_FIXTURE_CALLERS'
_PROJECT_VAR = 'THEMIS_GCP_PROJECT'

# The live upstreams' shared client: a generous default (VariantValidator self-extends per call).
_HTTP_TIMEOUT = httpx2.Timeout(30.0, connect=10.0)


@dataclasses.dataclass(frozen=True)
class Deps:
    """The image-level collaborators an interface's `register` is handed.

    Attributes:
        authorizer: What the image's auth interceptor resolves every call through
            (rpc-authorization.md): the session resolver, the caller verifier, and the one deployment
            fact admission turns on — the project that completes a `Caller`'s account id. The nine database-backed
            interfaces also resolve the session in the body through `authorizer.session_resolver`, a
            check the interceptor has made redundant.
        http_client: The client every live upstream call is issued on, held open for the server's
            lifetime by `stack`. It reaches only the hosts `upstreams.destinations` admits.
        stack: Owns whatever an interface's own adapter holds open for the server's lifetime — the
            GCS client and Cloud SQL connector `literature`'s corpus half builds. Nothing in the data
            plane handles SIGTERM, so it unwinds on a startup failure, not on a Cloud Run stop.
    """

    authorizer: interceptor_mod.Authorizer
    http_client: httpx2.AsyncClient
    stack: contextlib.AsyncExitStack


async def deps_from_env(stack: contextlib.AsyncExitStack) -> Deps:
    """Build the image's collaborators from the environment, or `SystemExit`.

    Args:
        stack: Owns the HTTP client for as long as the server runs.

    Returns:
        The collaborators, ready to hand to each interface's `register`.

    Raises:
        SystemExit: `THEMIS_AUTHORIZER_BACKEND` is unset or unknown, or the authorizer's config — the
            session seed, the caller verifier's seed, or the project — is missing or malformed.
    """
    return Deps(
        authorizer=_authorizer_from_env(),
        http_client=await stack.enter_async_context(destinations.admitting_client(timeout=_HTTP_TIMEOUT)),
        stack=stack,
    )


def _backend_from_env() -> str:
    backend = os.environ.get(_AUTHORIZER_VAR)
    if backend is None:
        raise SystemExit(f'{_AUTHORIZER_VAR} is required (expected "http" or "fixture")')
    if backend not in ('http', 'fixture'):
        raise SystemExit(f'unsupported {_AUTHORIZER_VAR} {backend!r} (expected "http" or "fixture")')
    return backend


def _authorizer_from_env() -> interceptor_mod.Authorizer:
    # Every input is read and validated before the http session resolver opens its channel, so a
    # deployment missing one fails on the missing name rather than on a connection attempt.
    backend = _backend_from_env()
    verify_caller = _caller_verifier_from_env(backend)
    project = _required(_PROJECT_VAR, 'the GCP project this service runs in, completing each Caller into an email')
    return interceptor_mod.Authorizer(
        session_resolver=_session_resolver_from_env(backend), verify_caller=verify_caller, project=project
    )


def _session_resolver_from_env(backend: str) -> session_mod.SessionResolver:
    if backend == 'http':
        return session_mod.session_resolver_from_env()
    return session_mod.fixture_session_resolver_from_json(
        os.environ.get(_FIXTURE_CONTEXTS_VAR), var_name=_FIXTURE_CONTEXTS_VAR
    )


def _caller_verifier_from_env(backend: str) -> caller_mod.CallerVerifier:
    if backend == 'http':
        return caller_mod.cloud_run_caller_verifier()
    return caller_mod.fixture_caller_verifier_from_json(
        os.environ.get(_FIXTURE_CALLERS_VAR), var_name=_FIXTURE_CALLERS_VAR
    )


def _required(var_name: str, what: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise SystemExit(f'{var_name} is required: {what}')
    return value
