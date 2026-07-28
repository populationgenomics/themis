"""The one-time consent, and the CLI wrapped around it.

Both are exercised offline: the browser flow is replaced, and the token endpoint is faked. The
scope test is the load-bearing one — the libraries reject a grant whose returned scopes differ
from the requested set, and no fake can discover that for us, so the real validator is driven
against the response Google actually sends.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat

import pytest
from oauthlib.oauth2.rfc6749 import errors as oauth_errors
from oauthlib.oauth2.rfc6749 import parameters

from themis.clients.iap import __main__ as iap_main
from themis.clients.iap import credentials
from themis.clients.iap.tests import fixture_pulumi, fixture_token


class _FixtureFlow:
    """Stands in for ``InstalledAppFlow``, recording the arguments consent was run with."""

    def __init__(self, refresh_token: str | None, refuses: Exception | None = None) -> None:
        self.refresh_token = refresh_token
        self.refuses = refuses
        self.server_kwargs: dict[str, object] = {}
        self.scopes: list[str] = []

    def from_client_config(self, client_config: dict[str, object], scopes: list[str]) -> _FixtureFlow:
        self.scopes = scopes
        return self

    def run_local_server(self, **kwargs: object) -> _FixtureFlow:
        self.server_kwargs = kwargs
        if self.refuses is not None:
            raise self.refuses
        return self


def _client() -> credentials.OAuthClient:
    return credentials.OAuthClient(client_id=fixture_token.CLIENT_ID, client_secret=fixture_token.CLIENT_SECRET)


def _consenting(
    monkeypatch: pytest.MonkeyPatch, refresh_token: str | None, refuses: Exception | None = None
) -> _FixtureFlow:
    flow = _FixtureFlow(refresh_token, refuses)
    monkeypatch.setattr(credentials.oauth_flow, 'InstalledAppFlow', flow)
    return flow


def _from_stack_config(monkeypatch: pytest.MonkeyPatch, refusal: str = '') -> fixture_pulumi.FixturePulumi:
    """Have the CLI read its client from a fixture stack, with a developer's own exports cleared."""
    for name in ('THEMIS_IAP_CLIENT_ID', 'THEMIS_IAP_CLIENT_SECRET', 'PULUMI_STACK'):
        monkeypatch.delenv(name, raising=False)
    values = fixture_pulumi.configured()
    if refusal:
        values |= {iap_main._CLIENTS_KEY: fixture_pulumi.Refusal(refusal)}
    return fixture_pulumi.answering(monkeypatch, values)


def test_the_requested_scopes_survive_the_granted_scope_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # oauthlib raises when the granted set differs from the requested one, and Google returns
    # `email` in canonical form. Requesting the shorthand makes every consent fail; only driving
    # the real validator against Google's actual response catches it. The scopes come off the
    # flow, so what consent asked for is what the validator checks.
    granted = json.dumps(
        {
            'access_token': 'at',
            'expires_in': 3599,
            'refresh_token': fixture_token.REFRESH_TOKEN,
            'token_type': 'Bearer',
            'id_token': fixture_token.unsigned_jwt(),
            'scope': fixture_token.GRANTED_SCOPE,
        }
    )
    flow = _consenting(monkeypatch, fixture_token.REFRESH_TOKEN)
    credentials.authorize(_client(), tmp_path / 'iap.json')

    parsed = parameters.parse_token_response(granted, scope=list(flow.scopes))

    assert not parsed.scope_changed


def test_consent_asks_for_a_reusable_grant(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # Both halves are load-bearing and neither may be left to a library default: `offline` is what
    # yields a refresh token, and an explicit consent prompt is what makes a re-login yield another.
    flow = _consenting(monkeypatch, fixture_token.REFRESH_TOKEN)

    credentials.authorize(_client(), tmp_path / 'iap.json')

    assert flow.server_kwargs['access_type'] == 'offline'
    assert flow.server_kwargs['prompt'] == 'consent'
    assert flow.server_kwargs['host'] == '127.0.0.1'
    assert flow.server_kwargs['port'] == 0
    assert flow.server_kwargs['timeout_seconds']


def test_consent_caches_the_grant_readable_only_by_its_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _consenting(monkeypatch, fixture_token.REFRESH_TOKEN)
    path = tmp_path / 'nested' / 'iap.json'

    stored = credentials.authorize(_client(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.load(path) == stored


def test_consent_without_a_refresh_token_fails_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # Caching a grant that cannot be replayed would defer the failure to the next run.
    _consenting(monkeypatch, None)
    path = tmp_path / 'iap.json'

    with pytest.raises(ValueError, match='no refresh token'):
        credentials.authorize(_client(), path)

    assert not path.exists()


@pytest.mark.parametrize(
    ('refuses', 'remedy'),
    [
        (credentials.oauth_flow.WSGITimeoutError('Timed out waiting for response'), 'not completed within'),
        (oauth_errors.AccessDeniedError(), 'refused the consent'),
        (oauth_errors.InvalidClientError(), 'do not belong to the same'),
    ],
    ids=['abandoned', 'declined', 'mismatched-halves'],
)
def test_a_consent_that_yields_nothing_names_its_remedy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, refuses: Exception, remedy: str
) -> None:
    # None of the three is a type `__main__` translates: `WSGITimeoutError` subclasses
    # `AttributeError`, the two `OAuth2Error`s `Exception` — so unconverted each reaches the
    # developer as a traceback.
    _consenting(monkeypatch, None, refuses=refuses)
    path = tmp_path / 'iap.json'

    with pytest.raises(ValueError, match=remedy):
        credentials.authorize(_client(), path)

    assert not path.exists()


def test_a_failed_write_leaves_no_partial_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # load() refuses a damaged cache rather than repairing it, so save() must never produce one.
    path = tmp_path / 'iap.json'
    path.write_text('{"client_id": "old", "client_secret": "old", "refresh_token": "old"}', 'utf-8')
    stored = credentials.StoredCredentials(client=_client(), refresh_token=fixture_token.REFRESH_TOKEN)

    def _fails(*args: object, **kwargs: object) -> None:
        raise OSError('no space left on device')

    monkeypatch.setattr(credentials.json, 'dump', _fails)

    with pytest.raises(OSError, match='no space left'):
        credentials.save(stored, path)

    assert credentials.load(path).refresh_token == 'old'
    assert list(tmp_path.iterdir()) == [path]


def test_leftover_debris_beside_the_cache_neither_blocks_nor_is_touched(tmp_path: pathlib.Path) -> None:
    # A process killed mid-write leaves a temp file behind, named by whatever scheme save() draws
    # from. None may refuse a later consent, and none is save()'s to clear — it removes only the
    # file it created. The pid form is the one a predictable scheme regenerates.
    path = tmp_path / 'iap.json'
    debris = [tmp_path / f'{path.name}.{os.getpid()}.tmp', tmp_path / f'{path.name}.abandoned.tmp']
    for leftover in debris:
        leftover.write_text('left behind', 'utf-8')
    stored = credentials.StoredCredentials(client=_client(), refresh_token=fixture_token.REFRESH_TOKEN)

    credentials.save(stored, path)

    assert credentials.load(path) == stored
    assert all(leftover.read_text('utf-8') == 'left behind' for leftover in debris)


def test_token_prints_only_the_token_on_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The runbook's `Bearer $(... token)` breaks the moment anything else lands on stdout.
    path = tmp_path / 'iap.json'
    token = fixture_token.unsigned_jwt()
    credentials.save(credentials.StoredCredentials(client=_client(), refresh_token=fixture_token.REFRESH_TOKEN), path)
    monkeypatch.setattr(iap_main.google_auth_requests, 'Request', lambda: fixture_token.granting(token))

    iap_main.main(['--cache', str(path), 'token'])

    assert capsys.readouterr().out == f'{token}\n'


def test_the_cli_reports_a_missing_consent_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(iap_main.google_auth_requests, 'Request', fixture_token.granting)

    with pytest.raises(SystemExit) as exit_info:
        iap_main.main(['--cache', str(tmp_path / 'absent.json'), 'token'])

    assert 'login' in str(exit_info.value)


def test_login_says_which_environment_it_consented_against(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Consenting against the wrong environment yields a token IAP refuses much later, by which
    # time nothing on screen says which one it was. The secret stays off both streams.
    _consenting(monkeypatch, fixture_token.REFRESH_TOKEN)
    _from_stack_config(monkeypatch)
    monkeypatch.setattr(
        iap_main.google_auth_requests, 'Request', lambda: fixture_token.granting(fixture_token.unsigned_jwt())
    )

    iap_main.main(['--cache', str(tmp_path / 'iap.json'), '--stack', 'prod', 'login'])

    printed = capsys.readouterr()
    assert 'prod' in printed.err
    assert fixture_token.CLIENT_ID in printed.err
    assert fixture_token.CLIENT_SECRET not in printed.err + printed.out


def test_an_unwritable_cache_reports_where_and_why(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # The one failure that lands after the consent is spent: a traceback here costs the developer
    # the browser round trip as well as the diagnosis.
    _consenting(monkeypatch, fixture_token.REFRESH_TOKEN)
    _from_stack_config(monkeypatch)
    path = tmp_path / 'iap.json'

    def _refuses(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, 'Permission denied')

    # Raised rather than staged with a mode: a test run as root ignores the bits.
    monkeypatch.setattr(credentials.tempfile, 'mkstemp', _refuses)

    with pytest.raises(SystemExit) as exit_info:
        iap_main.main(['--cache', str(path), 'login'])

    assert 'could not be read or written' in str(exit_info.value)
    assert str(path) in str(exit_info.value)


def test_login_never_consents_to_a_client_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # A consent run against a half-formed client would spend the developer's browser round trip
    # on a grant IAP refuses, blaming the client rather than the unread config.
    flow = _consenting(monkeypatch, fixture_token.REFRESH_TOKEN)
    _from_stack_config(monkeypatch, refusal=fixture_pulumi.NOT_LOGGED_IN)
    path = tmp_path / 'iap.json'

    with pytest.raises(SystemExit) as exit_info:
        iap_main.main(['--cache', str(path), 'login'])

    assert 'pulumi login' in str(exit_info.value)
    assert not flow.server_kwargs
    assert not path.exists()


def test_token_refuses_a_stack_it_cannot_honour(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One cache holds one consent, whichever environment it was for. Accepting --stack here would
    # print a token for the environment last consented to while naming another. The same call
    # without the flag succeeds, so the refusal is the flag's and not the fixture's.
    path = tmp_path / 'iap.json'
    credentials.save(credentials.StoredCredentials(client=_client(), refresh_token=fixture_token.REFRESH_TOKEN), path)
    monkeypatch.setattr(
        iap_main.google_auth_requests, 'Request', lambda: fixture_token.granting(fixture_token.unsigned_jwt())
    )
    iap_main.main(['--cache', str(path), 'token'])

    with pytest.raises(SystemExit) as exit_info:
        iap_main.main(['--cache', str(path), '--stack', 'prod', 'token'])

    assert exit_info.value.code == 2
    assert '--stack' in capsys.readouterr().err


def test_the_cli_reports_a_damaged_cache_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    path = tmp_path / 'iap.json'
    path.write_text('truncated', 'utf-8')
    monkeypatch.setattr(iap_main.google_auth_requests, 'Request', fixture_token.granting)

    with pytest.raises(SystemExit) as exit_info:
        iap_main.main(['--cache', str(path), 'token'])

    assert 'delete it' in str(exit_info.value)
