"""The consent cache and the refresh-token exchange, driven offline.

What has to hold: a token IAP would refuse never reaches a caller, the cache stays unreadable
by anyone but its owner, and each failure a developer hits names its remedy.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import stat
import urllib.parse

import pytest

from themis.clients.iap import credentials
from themis.clients.iap.tests import fixture_token


def _stored() -> credentials.StoredCredentials:
    return credentials.StoredCredentials(
        client=credentials.OAuthClient(client_id=fixture_token.CLIENT_ID, client_secret=fixture_token.CLIENT_SECRET),
        refresh_token=fixture_token.REFRESH_TOKEN,
    )


def test_mint_returns_the_identity_and_expiry_the_caller_gates_on() -> None:
    transport = fixture_token.granting(fixture_token.unsigned_jwt())

    token = credentials.mint(_stored(), transport)

    assert token.email == fixture_token.EMAIL
    assert not token.expired(datetime.datetime.now(tz=datetime.UTC))


def test_mint_spends_the_stored_consent_and_asks_for_an_identity_scope() -> None:
    # Google issues an ID token only when the grant carries an identity scope, and pins its `aud`
    # to the client named here. A fake cannot model either rule, so the request itself is asserted.
    transport = fixture_token.granting(fixture_token.unsigned_jwt())

    credentials.mint(_stored(), transport)

    (call,) = transport.calls
    body = call['body']
    assert isinstance(body, bytes)
    sent = urllib.parse.parse_qs(body.decode('utf-8'))
    assert sent['grant_type'] == ['refresh_token']
    assert sent['client_id'] == [fixture_token.CLIENT_ID]
    assert sent['refresh_token'] == [fixture_token.REFRESH_TOKEN]
    assert 'openid' in sent['scope'][0].split()


def test_mint_rejects_a_token_iap_would_not_admit() -> None:
    # IAP admits a token only when `aud` is an allowlisted client. Surfacing a mismatch here
    # names the cause; letting it through surfaces as an opaque 401 from the load balancer.
    transport = fixture_token.granting(fixture_token.unsigned_jwt(audience='someone-else.apps.googleusercontent.com'))

    with pytest.raises(ValueError, match='audience'):
        credentials.mint(_stored(), transport)


def test_mint_rejects_a_token_without_the_email_claim() -> None:
    transport = fixture_token.granting(fixture_token.unsigned_jwt(email=None))

    with pytest.raises(ValueError, match='email'):
        credentials.mint(_stored(), transport)


def test_mint_rejects_a_grant_that_returned_no_id_token() -> None:
    with pytest.raises(ValueError, match='no ID token'):
        credentials.mint(_stored(), fixture_token.granting())


def test_mint_maps_a_refused_grant_to_a_remedy() -> None:
    with pytest.raises(credentials.ConsentRejectedError, match='login'):
        credentials.mint(_stored(), fixture_token.refusing())


@pytest.mark.parametrize(
    ('expires_in', 'spent'),
    [(3600, False), (int(credentials.EXPIRY_MARGIN.total_seconds()) - 1, True), (-1, True)],
)
def test_a_token_inside_the_expiry_margin_counts_as_spent(expires_in: int, spent: bool) -> None:
    # A token handed out this close to expiry can die in flight, arriving at IAP as a rejection
    # indistinguishable from a revoked consent.
    token = credentials.mint(_stored(), fixture_token.granting(fixture_token.unsigned_jwt(expires_in=expires_in)))

    assert token.expired(datetime.datetime.now(tz=datetime.UTC)) is spent


def test_saved_credentials_are_readable_only_by_their_owner(tmp_path: pathlib.Path) -> None:
    path = tmp_path / 'nested' / 'iap.json'

    credentials.save(_stored(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_replaces_a_wide_cache_with_an_owner_only_one(tmp_path: pathlib.Path) -> None:
    # The rename discards the old inode along with its mode, so a cache someone else wrote wide
    # does not survive a save.
    path = tmp_path / 'iap.json'
    path.write_text('{}', 'utf-8')
    path.chmod(0o644)

    credentials.save(_stored(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_saved_credentials_round_trip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / 'iap.json'
    stored = _stored()

    credentials.save(stored, path)

    assert credentials.load(path) == stored


def test_load_without_a_cache_asks_for_consent(tmp_path: pathlib.Path) -> None:
    with pytest.raises(credentials.ConsentRequiredError, match='login'):
        credentials.load(tmp_path / 'absent.json')


@pytest.mark.parametrize(
    'content',
    ['not json', '[]', json.dumps({'client_id': fixture_token.CLIENT_ID, 'client_secret': 'x'})],
)
def test_load_refuses_a_damaged_cache_rather_than_reprompting(tmp_path: pathlib.Path, content: str) -> None:
    # A half-written cache is an error to surface: silently re-consenting would hide whatever
    # truncated it.
    path = tmp_path / 'iap.json'
    path.write_text(content, 'utf-8')

    with pytest.raises(ValueError, match='delete it'):
        credentials.load(path)


def test_default_cache_path_honours_an_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    override = tmp_path / 'elsewhere' / 'iap.json'
    monkeypatch.setenv('THEMIS_IAP_CREDENTIALS', str(override))

    assert credentials.default_cache_path() == override


def test_default_cache_path_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.delenv('THEMIS_IAP_CREDENTIALS', raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))

    assert credentials.default_cache_path() == tmp_path / 'themis' / 'iap.json'
