"""Request issuing and the translation of each refusal into a remedy.

The three gates refuse differently — IAP on the audience, IAP on the identity, the app on
``project_members`` — and the third is deliberately indistinguishable from a missing path. Each
has to arrive at the developer as something to go and do.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from themis.clients.iap import client as iap_client
from themis.clients.iap import credentials
from themis.clients.iap.tests import fixture_app, fixture_token

_BASE_URL = 'https://themis-dev.example.org'


def _stored() -> credentials.StoredCredentials:
    return credentials.StoredCredentials(
        client=credentials.OAuthClient(client_id=fixture_token.CLIENT_ID, client_secret=fixture_token.CLIENT_SECRET),
        refresh_token=fixture_token.REFRESH_TOKEN,
    )


def _client(
    status: int, body: str = '', transport: fixture_token.FixtureTransport | None = None
) -> tuple[iap_client.Client, fixture_app.FixtureAdapter]:
    granting = transport if transport is not None else fixture_token.granting(fixture_token.unsigned_jwt())
    session, adapter = fixture_app.session(status, body)
    return iap_client.Client(_BASE_URL, _stored(), request=granting, session=session), adapter


def test_a_request_bears_the_minted_token_as_a_bearer_credential() -> None:
    # IAP reads the ID token off `Authorization`; anywhere else it is invisible to the gate.
    token = fixture_token.unsigned_jwt()
    client, adapter = _client(200, transport=fixture_token.granting(token))

    client.get('/api/projects')

    (sent,) = adapter.calls
    assert sent.headers['Authorization'] == f'Bearer {token}'
    assert sent.url == f'{_BASE_URL}/api/projects'


def test_a_post_carries_its_body_as_json() -> None:
    client, adapter = _client(201)

    client.post('/api/analyses', {'projectId': 'p1', 'prompt': 'hello'})

    (sent,) = adapter.calls
    assert sent.headers['Content-Type'] == 'application/json'
    body = sent.body
    assert isinstance(body, bytes)
    assert json.loads(body) == {'projectId': 'p1', 'prompt': 'hello'}


def test_a_request_carries_a_timeout() -> None:
    # `requests` has no default; a smoke check that hangs is worse than one that fails.
    client, adapter = _client(200)

    client.get('/api/projects')

    assert adapter.timeouts == [pytest.approx(30.0)]


def test_a_relative_path_fails_loud() -> None:
    # Without the leading slash the base URL and the path silently fuse into another host.
    client, adapter = _client(200)

    with pytest.raises(ValueError, match='must start with'):
        client.get('api/projects')

    assert adapter.calls == []


@pytest.mark.parametrize('origin', ['http://themis-dev.example.org', 'themis-dev.example.org'])
def test_a_cleartext_origin_fails_loud(origin: str) -> None:
    # Every request carries a live ID token as a bearer credential; a mistyped scheme would put
    # one on the wire in the clear, and no response status reveals that it happened.
    with pytest.raises(ValueError, match='https origin'):
        iap_client.Client(origin, _stored())


def test_a_sign_in_redirect_is_reported_as_a_refusal() -> None:
    # Followed, it would yield an HTML sign-in page under a 200 and look like success.
    client, _ = _client(302)

    with pytest.raises(iap_client.AccessRefusedError, match='redirect'):
        client.get('/api/projects')


def test_a_permitted_response_is_returned_whatever_its_status() -> None:
    # Only the statuses that mean "you cannot reach this" are raised; the app's own errors are
    # the caller's to read.
    client, _ = _client(500)

    assert client.get('/api/projects').status_code == 500


def test_an_unadmitted_audience_names_the_consent_it_spent() -> None:
    # Two causes reach this status: a consent for another environment, and an allowlist not yet
    # applied. The audience is what tells them apart, and only one of them is reachable in steady
    # state — a message naming only the other sends the reader to check a months-old deploy.
    client, _ = _client(401, 'Invalid IAP credentials')

    with pytest.raises(iap_client.AccessRefusedError, match='iapProgrammaticClients') as refusal:
        client.get('/api/projects')

    assert fixture_token.CLIENT_ID in str(refusal.value)
    assert _BASE_URL in str(refusal.value)


def test_an_unadmitted_identity_points_at_the_access_group() -> None:
    client, _ = _client(403)

    with pytest.raises(iap_client.AccessRefusedError, match='access group'):
        client.get('/api/projects')


def test_not_found_names_the_membership_row_it_may_really_mean() -> None:
    # The app answers a non-member with not-found so a caller cannot learn an analysis exists.
    # A client reporting a bare 404 would send the developer hunting for a typo instead.
    client, _ = _client(404)

    with pytest.raises(iap_client.NotProvisionedError, match='project_members'):
        client.get('/api/analyses?project=p1')


@pytest.mark.parametrize(
    ('status', 'error'),
    [(401, iap_client.AccessRefusedError), (403, iap_client.AccessRefusedError), (404, iap_client.NotProvisionedError)],
)
def test_every_refusal_names_the_identity_that_was_refused(status: int, error: type[Exception]) -> None:
    # Which of a developer's Google accounts consented is exactly what they get wrong.
    client, _ = _client(status)

    with pytest.raises(error, match=fixture_token.EMAIL):
        client.get('/api/projects')


def test_a_live_token_is_reused_across_requests() -> None:
    # Minting is a network round trip to Google; re-spending the refresh token per request would
    # make a smoke test's cost linear in its size for nothing.
    transport = fixture_token.granting(fixture_token.unsigned_jwt())
    client, _ = _client(200, transport=transport)

    client.get('/api/projects')
    client.get('/api/projects')

    assert len(transport.calls) == 1


def test_a_spent_token_is_replaced_before_the_next_request() -> None:
    # A run outlasting a token's hour must not start presenting a dead one.
    transport = fixture_token.granting(fixture_token.unsigned_jwt(expires_in=1))
    client, _ = _client(200, transport=transport)

    client.get('/api/projects')
    client.get('/api/projects')

    assert len(transport.calls) == 2


def test_from_cache_without_consent_asks_for_it(tmp_path: pathlib.Path) -> None:
    with pytest.raises(credentials.ConsentRequiredError, match='login'):
        iap_client.from_cache(_BASE_URL, tmp_path / 'absent.json')
