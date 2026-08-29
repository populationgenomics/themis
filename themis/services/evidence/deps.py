"""What the evidence image builds once and every interface that needs it is handed.

Two things are the image's rather than any one interface's. Authorization is the same wherever it
applies — the same session token, resolved through the same auth service — so an image-wide
`THEMIS_AUTHORIZER_BACKEND` selects it, not a per-interface copy of the same value: every interface
resolves a session somewhere, and a resolver each would hold ten idle gRPC channels to auth in place of
one. And the nine database-backed interfaces reach public HTTP upstreams, as does `literature`'s
discovery half, so they share one `httpx.AsyncClient`: a client each would be ten connection pools
against overlapping hosts.

Everything else stays the interface's own — which adapter its port builds, and the vars that
configure it (`services.md`, "One deployment, several interfaces").
"""

from __future__ import annotations

import contextlib
import dataclasses
import os

import httpx

from themis.clients.auth import session as session_mod

_AUTHORIZER_VAR = 'THEMIS_AUTHORIZER_BACKEND'
_FIXTURE_CONTEXTS_VAR = 'THEMIS_EVIDENCE_FIXTURE_CONTEXTS'

# The live upstreams' shared client: a generous default (VariantValidator self-extends per call).
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclasses.dataclass(frozen=True)
class Deps:
    """The image-level collaborators an interface's `register` is handed.

    Attributes:
        session_resolver: Resolves a request's session token to its binding. Every evidence interface
            authorizes through it — nine on every rpc, `literature` on the one step that spends money.
        http_client: The client every live upstream call is issued on, held open for the server's
            lifetime by `stack`.
        stack: Owns whatever an interface's own adapter holds open for the server's lifetime — the
            GCS client and Cloud SQL connector `literature`'s corpus half builds. Nothing in the data
            plane handles SIGTERM, so it unwinds on a startup failure, not on a Cloud Run stop.
    """

    session_resolver: session_mod.SessionResolver
    http_client: httpx.AsyncClient
    stack: contextlib.AsyncExitStack


async def deps_from_env(stack: contextlib.AsyncExitStack) -> Deps:
    """Build the image's collaborators from the environment, or `SystemExit`.

    Args:
        stack: Owns the HTTP client for as long as the server runs.

    Returns:
        The collaborators, ready to hand to each interface's `register`.

    Raises:
        SystemExit: `THEMIS_AUTHORIZER_BACKEND` is unset or unknown, or the fixture authorizer's seed
            is missing or malformed.
    """
    return Deps(
        session_resolver=_session_resolver_from_env(),
        http_client=await stack.enter_async_context(httpx.AsyncClient(timeout=_HTTP_TIMEOUT)),
        stack=stack,
    )


def _session_resolver_from_env() -> session_mod.SessionResolver:
    backend = os.environ.get(_AUTHORIZER_VAR)
    if backend is None:
        raise SystemExit(f'{_AUTHORIZER_VAR} is required (expected "http" or "fixture")')
    if backend == 'http':
        return session_mod.session_resolver_from_env()
    if backend == 'fixture':
        return session_mod.fixture_session_resolver_from_json(
            os.environ.get(_FIXTURE_CONTEXTS_VAR), var_name=_FIXTURE_CONTEXTS_VAR
        )
    raise SystemExit(f'unsupported {_AUTHORIZER_VAR} {backend!r} (expected "http" or "fixture")')
