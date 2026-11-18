"""What the laptop tools take from Google is refused unless it is what a gated service and a caller need."""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import time
from typing import override

import pytest
import requests
import requests.adapters
from google.auth import credentials as credentials_mod
from google.auth import jwt
from google.auth.transport import requests as auth_requests

from tools import clu

_ACCOUNT = 'clu@x.iam.gserviceaccount.com'
_AUDIENCE = 'https://svc.example'
_IAM_IDTOKEN_URL = f'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{_ACCOUNT}:generateIdToken'
_IAM_SIGN_URL = f'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{_ACCOUNT}:signBlob'
_RUN_SERVICE_URL = 'https://run.googleapis.com/v2/projects/p/locations/australia-southeast1/services/themis-sheaf'
_USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'


def _jwt(claims: dict[str, object]) -> str:
    full_claims = {'exp': int(time.time()) + 3600, **claims}

    def segment(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')

    return f'{segment({"alg": "RS256"})}.{segment(full_claims)}.c2lnbmF0dXJl'


class _SourceLogin(credentials_mod.Credentials):
    def __init__(self) -> None:
        super().__init__()
        self.token = 'source-access-token'  # noqa: S105 — fixture token
        self.expiry = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=1)

    @override
    def refresh(self, request: object) -> None:
        del request


def _response(status: int, body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers['Content-Type'] = 'application/json'
    response.raw = io.BytesIO(body.encode('utf-8'))
    return response


@pytest.fixture
def endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]]:
    """Queue expected ``(method, url, answer)`` HTTP exchanges and record requested quota projects."""
    queued: list[tuple[str, str, requests.Response | requests.RequestException]] = []
    quota_projects: list[str | None] = []

    def send(_adapter: object, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        assert queued, f'unanticipated HTTP request: {request.method} {request.url}'
        expected_method, expected_url, answer = queued.pop(0)
        assert request.method == expected_method
        assert request.url == expected_url
        if isinstance(answer, requests.RequestException):
            raise answer
        answer.request = request
        answer.url = request.url or ''
        return answer

    def caller_credentials(*, quota_project: str | None) -> credentials_mod.Credentials:
        quota_projects.append(quota_project)
        return _SourceLogin()

    monkeypatch.setattr(clu, 'caller_credentials', caller_credentials)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    return queued, quota_projects


def test_a_token_carrying_the_accounts_email_is_returned(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
) -> None:
    queued, quota_projects = endpoints
    token = _jwt({'aud': _AUDIENCE, 'email': _ACCOUNT})
    answer = _response(200, json.dumps({'token': token}))
    queued.append(('POST', _IAM_IDTOKEN_URL, answer))

    assert clu.identity_token(_ACCOUNT, _AUDIENCE) == token
    assert quota_projects == [None]
    assert answer.request is not None
    raw_body = answer.request.body
    assert isinstance(raw_body, bytes)
    sent = json.loads(raw_body.decode('utf-8'))
    assert sent['audience'] == _AUDIENCE
    assert sent['includeEmail'] is True


@pytest.mark.parametrize(
    'claims', [{'aud': _AUDIENCE}, {'aud': _AUDIENCE, 'email': 'someone@x.iam.gserviceaccount.com'}]
)
def test_a_token_not_naming_the_account_by_email_is_refused(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
    claims: dict[str, object],
) -> None:
    queued, _ = endpoints
    queued.append(('POST', _IAM_IDTOKEN_URL, _response(200, json.dumps({'token': _jwt(claims)}))))
    with pytest.raises(clu.GoogleCallError, match='carries email'):
        clu.identity_token(_ACCOUNT, _AUDIENCE)


@pytest.mark.parametrize(
    ('answer', 'match'),
    [
        (_response(403, json.dumps({'error': {'message': 'getOpenIdToken denied'}})), 'getOpenIdToken denied'),
        (requests.ConnectionError('connection reset'), 'connection reset'),
    ],
)
def test_a_failed_mint_is_a_google_call_error_in_its_own_words(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
    answer: requests.Response | requests.RequestException,
    match: str,
) -> None:
    queued, _ = endpoints
    queued.append(('POST', _IAM_IDTOKEN_URL, answer))
    with pytest.raises(clu.GoogleCallError, match=match):
        clu.identity_token(_ACCOUNT, _AUDIENCE)


def test_iap_jwt_names_the_origin_wildcard_and_the_account(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
) -> None:
    queued, quota_projects = endpoints
    signed = base64.b64encode(b'iam-signature').decode('ascii')
    queued.append(('POST', _IAM_SIGN_URL, _response(200, json.dumps({'signedBlob': signed}))))

    creds = clu.iap_jwt_credentials(_ACCOUNT, 'https://themis.example/')
    creds.refresh(auth_requests.Request())

    assert quota_projects == [None]
    assert creds.token is not None
    claims = jwt.decode(creds.token, verify=False)
    assert claims['iss'] == _ACCOUNT
    assert claims['sub'] == _ACCOUNT
    assert claims['aud'] == 'https://themis.example/*'


_BUILT_URL = 'https://themis-sheaf-123456789.australia-southeast1.run.app'
_HASHED_URL = 'https://themis-sheaf-abcdefghij-ts.a.run.app'


def test_the_service_url_is_the_one_built_from_its_name(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
) -> None:
    # The hashed URL is the `uri`; the services accept ID tokens only for the built one.
    queued, quota_projects = endpoints
    queued.append(
        ('GET', _RUN_SERVICE_URL, _response(200, json.dumps({'uri': _HASHED_URL, 'urls': [_BUILT_URL, _HASHED_URL]})))
    )
    assert clu.run_service_url('themis-sheaf', project='p') == _BUILT_URL
    assert quota_projects == ['p']


@pytest.mark.parametrize(
    ('status', 'body', 'match'),
    [
        (200, json.dumps({'uri': _HASHED_URL, 'urls': [_HASHED_URL]}), 'no single URL'),
        (200, json.dumps({'urls': ['http://themis-sheaf-123456789.australia-southeast1.run.app']}), 'no single URL'),
        (
            200,
            json.dumps({'urls': ['https://themis-sheaf-evil-123456789.australia-southeast1.run.app']}),
            'no single URL',
        ),
        (200, json.dumps({'name': 'themis-sheaf'}), 'no single URL'),
        (200, json.dumps({'urls': 7}), 'no single URL'),
        (200, '<html>', 'not JSON'),
        (200, '[]', 'not a JSON object'),
        (403, '{"error": {"message": "denied"}}', 'HTTP 403.*denied'),
        (404, '{"error": {"message": "not found"}}', 'HTTP 404'),
    ],
)
def test_a_service_without_a_usable_url_is_refused(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
    status: int,
    body: str,
    match: str,
) -> None:
    queued, _ = endpoints
    queued.append(('GET', _RUN_SERVICE_URL, _response(status, body)))
    with pytest.raises(clu.GoogleCallError, match=match):
        clu.run_service_url('themis-sheaf', project='p')


def test_an_unreachable_api_is_a_google_call_error(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
) -> None:
    queued, _ = endpoints
    queued.append(('GET', _RUN_SERVICE_URL, requests.ConnectionError('connection refused')))
    with pytest.raises(clu.GoogleCallError, match='connection refused'):
        clu.run_service_url('themis-sheaf', project='p')


def test_the_caller_email_is_the_logins_verified_email(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
) -> None:
    queued, quota_projects = endpoints
    queued.append(
        ('GET', _USERINFO_URL, _response(200, json.dumps({'email': 'me@example.org', 'email_verified': True})))
    )
    assert clu.caller_email() == 'me@example.org'
    assert quota_projects == [None]


@pytest.mark.parametrize(
    'userinfo', [{'sub': '1'}, {'email': 'me@example.org'}, {'email': 'me@example.org', 'email_verified': False}]
)
def test_a_login_without_a_verified_email_is_refused(
    endpoints: tuple[list[tuple[str, str, requests.Response | requests.RequestException]], list[str | None]],
    userinfo: dict[str, object],
) -> None:
    queued, _ = endpoints
    queued.append(('GET', _USERINFO_URL, _response(200, json.dumps(userinfo))))
    with pytest.raises(clu.GoogleCallError, match='no verified email'):
        clu.caller_email()
