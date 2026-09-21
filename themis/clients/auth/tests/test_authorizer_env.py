"""``authorizer_from_env``: the selector, the seeds, and the project, each required and named when missing."""

from __future__ import annotations

import asyncio

import pytest

from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2

_CONTEXTS_VAR = 'TEST_FIXTURE_CONTEXTS'
_CALLERS_VAR = 'TEST_FIXTURE_CALLERS'
_FIXTURE_ENV = {
    'THEMIS_AUTHORIZER_BACKEND': 'fixture',
    'THEMIS_GCP_PROJECT': 'x',
    _CONTEXTS_VAR: '{"tok": {"project_id": "p", "analysis_id": "a"}}',
    _CALLERS_VAR: '{"bearer": "themis-web@x.iam.gserviceaccount.com"}',
}


def _build() -> interceptor_mod.Authorizer:
    return interceptor_mod.authorizer_from_env(fixture_contexts_var=_CONTEXTS_VAR, fixture_callers_var=_CALLERS_VAR)


def _seed(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**_FIXTURE_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_the_fixture_authorizer_resolves_its_seeded_session_in_the_stated_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)
    authorizer = _build()

    async def resolve(token: str) -> auth_pb2.SessionContext:
        return await authorizer.session_resolver(token)

    context = asyncio.run(resolve('tok'))
    assert (context.project_id, context.analysis_id) == ('p', 'a')
    assert authorizer.project == 'x'
    with pytest.raises(session_mod.UnresolvedSessionError):
        asyncio.run(resolve('unknown'))


@pytest.mark.parametrize('missing', ['THEMIS_AUTHORIZER_BACKEND', 'THEMIS_GCP_PROJECT', _CONTEXTS_VAR, _CALLERS_VAR])
def test_every_input_is_required_and_named_when_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _seed(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        _build()


def test_an_unknown_selector_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch, THEMIS_AUTHORIZER_BACKEND='ldap')
    with pytest.raises(SystemExit, match='ldap'):
        _build()


def test_the_http_authorizer_needs_the_auth_service_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch, THEMIS_AUTHORIZER_BACKEND='http')
    monkeypatch.delenv('THEMIS_AUTH_URL', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_AUTH_URL'):
        _build()
