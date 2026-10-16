"""What the laptop tools take from Google is refused unless it is what a gated service and a caller need."""

from __future__ import annotations

import base64
import io
import json

import pytest
import requests
import requests.adapters
from google.auth import credentials as credentials_mod
from google.auth import exceptions

from tools import clu

_ACCOUNT = 'clu@x.iam.gserviceaccount.com'
_AUDIENCE = 'https://svc.example'


def _jwt(claims: dict[str, object]) -> str:
    def segment(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')

    return f'{segment({"alg": "RS256"})}.{segment(claims)}.c2lnbmF0dXJl'


def _minting(monkeypatch: pytest.MonkeyPatch, *, token: str | None = None, error: Exception | None = None) -> None:
    """Stand in for the IAM Credentials API: every mint answers `token`, or fails with `error`."""

    class IDTokenCredentials:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            self.token: str | None = None

        def refresh(self, request: object) -> None:
            del request
            if error is not None:
                raise error
            self.token = token

    monkeypatch.setattr(clu, 'impersonated', lambda service_account: service_account)
    monkeypatch.setattr(clu.impersonated_credentials, 'IDTokenCredentials', IDTokenCredentials)


def test_a_token_carrying_the_accounts_email_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _jwt({'aud': _AUDIENCE, 'email': _ACCOUNT})
    _minting(monkeypatch, token=token)
    assert clu.identity_token(_ACCOUNT, _AUDIENCE) == token


@pytest.mark.parametrize(
    'claims', [{'aud': _AUDIENCE}, {'aud': _AUDIENCE, 'email': 'someone@x.iam.gserviceaccount.com'}]
)
def test_a_token_not_naming_the_account_by_email_is_refused(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, object]
) -> None:
    _minting(monkeypatch, token=_jwt(claims))
    with pytest.raises(clu.GoogleCallError, match='carries email'):
        clu.identity_token(_ACCOUNT, _AUDIENCE)


@pytest.mark.parametrize(
    'error', [exceptions.RefreshError('getOpenIdToken denied'), requests.ConnectionError('connection reset')]
)
def test_a_failed_mint_is_a_google_call_error_in_its_own_words(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _minting(monkeypatch, error=error)
    with pytest.raises(clu.GoogleCallError, match=str(error)):
        clu.identity_token(_ACCOUNT, _AUDIENCE)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> list[requests.Response | requests.RequestException]:
    """Stand in for Google's HTTP endpoints: each request takes the next queued response, or raises it."""
    queued: list[requests.Response | requests.RequestException] = []

    def send(adapter: object, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        del adapter, kwargs
        answer = queued.pop(0)
        if isinstance(answer, requests.RequestException):
            raise answer
        answer.request = request
        answer.url = request.url or ''
        return answer

    def caller_credentials(*, quota_project: str | None) -> credentials_mod.Credentials:
        del quota_project
        return credentials_mod.AnonymousCredentials()

    monkeypatch.setattr(clu, 'caller_credentials', caller_credentials)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    return queued


def _response(status: int, body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.raw = io.BytesIO(body.encode())
    return response


def test_the_service_url_is_the_services_uri(answers: list[requests.Response | requests.RequestException]) -> None:
    answers.append(_response(200, json.dumps({'name': 'themis-sheaf', 'uri': 'https://sheaf.example.run.app'})))
    assert clu.run_service_url('themis-sheaf', project='p') == 'https://sheaf.example.run.app'


@pytest.mark.parametrize(
    ('status', 'body', 'match'),
    [
        (200, json.dumps({'uri': 'http://sheaf.example.run.app'}), 'no https URL'),
        (200, json.dumps({'name': 'themis-sheaf'}), 'no https URL'),
        (200, json.dumps({'uri': 7}), 'no https URL'),
        (200, '<html>', 'not JSON'),
        (200, '[]', 'not a JSON object'),
        (403, '{"error": {"message": "denied"}}', 'HTTP 403.*denied'),
        (404, '{"error": {"message": "not found"}}', 'HTTP 404'),
    ],
)
def test_a_service_without_a_usable_url_is_refused(
    answers: list[requests.Response | requests.RequestException], status: int, body: str, match: str
) -> None:
    answers.append(_response(status, body))
    with pytest.raises(clu.GoogleCallError, match=match):
        clu.run_service_url('themis-sheaf', project='p')


def test_an_unreachable_api_is_a_google_call_error(
    answers: list[requests.Response | requests.RequestException],
) -> None:
    answers.append(requests.ConnectionError('connection refused'))
    with pytest.raises(clu.GoogleCallError, match='connection refused'):
        clu.run_service_url('themis-sheaf', project='p')


def test_the_caller_email_is_the_logins_verified_email(
    answers: list[requests.Response | requests.RequestException],
) -> None:
    answers.append(_response(200, json.dumps({'email': 'me@example.org', 'email_verified': True})))
    assert clu.caller_email() == 'me@example.org'


@pytest.mark.parametrize(
    'userinfo', [{'sub': '1'}, {'email': 'me@example.org'}, {'email': 'me@example.org', 'email_verified': False}]
)
def test_a_login_without_a_verified_email_is_refused(
    answers: list[requests.Response | requests.RequestException], userinfo: dict[str, object]
) -> None:
    answers.append(_response(200, json.dumps(userinfo)))
    with pytest.raises(clu.GoogleCallError, match='no verified email'):
        clu.caller_email()
