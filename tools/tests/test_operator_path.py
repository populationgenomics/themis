"""The operator's path through IAP: what each call carries, and what an IAP refusal is reported as."""

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
from google.auth import jwt as google_jwt

from tools import clu, operator_path

_ACCOUNT = 'clu@example.iam.gserviceaccount.com'
_IAM_IDTOKEN_URL = f'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{_ACCOUNT}:generateIdToken'
_IAM_SIGN_URL = f'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{_ACCOUNT}:signBlob'
_TARGET = operator_path.Target(
    origin='https://themis.example', service_url='https://sheaf.example', grpc_service='themis.rpc.sheaf.Sheaf'
)


def _jwt(exp: float, *, claims: dict[str, object] | None = None) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp, **(claims or {})}).encode()).decode().rstrip('=')
    return f'eyJhbGciOiJSUzI1NiJ9.{payload}.c2lnbmF0dXJl'


def test_jwt_expiry_reads_the_exp_claim() -> None:
    assert operator_path.jwt_expiry(_jwt(1_800_000_000)) == 1_800_000_000


@pytest.mark.parametrize('token', ['not-a-jwt', 'a.b', 'a.!!!.c', f'a.{base64.urlsafe_b64encode(b"{}").decode()}.c'])
def test_jwt_expiry_refuses_what_is_not_a_token_that_expires(token: str) -> None:
    with pytest.raises(ValueError, match='not a'):
        operator_path.jwt_expiry(token)


@pytest.mark.parametrize(
    ('origin', 'service_url'),
    [('http://themis.example', 'https://sheaf.example'), ('https://themis.example', 'sheaf.example')],
)
def test_a_target_is_https_on_both_hops(origin: str, service_url: str) -> None:
    with pytest.raises(ValueError, match='https://'):
        operator_path.Target(origin=origin, service_url=service_url, grpc_service='themis.rpc.sheaf.Sheaf')


def test_a_target_dials_the_load_balancer() -> None:
    assert _TARGET.grpc_target == 'themis.example:443'


class _SourceLogin(credentials_mod.Credentials):
    def __init__(self) -> None:
        super().__init__()
        self.token = 'source-access-token'  # noqa: S105 — fixture token
        self.expiry = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=1)

    @override
    def refresh(self, request: object) -> None:
        del request


def test_every_call_carries_the_service_token_and_the_iap_token(monkeypatch: pytest.MonkeyPatch) -> None:
    service_bearer = _jwt(int(time.time()) + 3600, claims={'aud': _TARGET.service_url, 'email': _ACCOUNT})
    idtoken_bodies: list[dict[str, object]] = []
    quota_projects: list[str | None] = []

    def fake_caller_credentials(*, quota_project: str | None = None) -> _SourceLogin:
        quota_projects.append(quota_project)
        return _SourceLogin()

    def send(_adapter: object, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        assert request.method == 'POST'
        if request.url == _IAM_IDTOKEN_URL:
            assert isinstance(request.body, bytes)
            idtoken_bodies.append(json.loads(request.body.decode('utf-8')))
            return _response(200, json.dumps({'token': service_bearer}))
        if request.url == _IAM_SIGN_URL:
            signed = base64.b64encode(b'iap-sig').decode('ascii')
            return _response(200, json.dumps({'signedBlob': signed}))
        raise AssertionError(f'unanticipated IAM call: {request.url}')

    monkeypatch.setattr(clu, 'caller_credentials', fake_caller_credentials)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)

    metadata = dict(operator_path.Tokens(_TARGET, _ACCOUNT).metadata())

    assert quota_projects == [None, None]
    assert set(metadata) == {'authorization', 'proxy-authorization'}
    assert metadata['authorization'] == f'Bearer {service_bearer}'
    assert idtoken_bodies[0]['audience'] == _TARGET.service_url
    assert idtoken_bodies[0]['includeEmail'] is True

    proxy_token = metadata['proxy-authorization'].removeprefix('Bearer ')
    assert proxy_token != service_bearer
    iap_claims = google_jwt.decode(proxy_token, verify=False)
    assert iap_claims['iss'] == _ACCOUNT
    assert iap_claims['sub'] == _ACCOUNT
    assert iap_claims['aud'] == f'{_TARGET.origin}/*'


@pytest.fixture
def answer(monkeypatch: pytest.MonkeyPatch) -> list[requests.Response]:
    """Stand in for the load balancer: the probe takes the queued response, and the request is recorded on it."""
    queued: list[requests.Response] = []

    def send(adapter: object, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        del adapter, kwargs
        response = queued.pop(0)
        response.request = request
        response.url = request.url or ''
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    return queued


def _response(status: int, body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers['Content-Type'] = 'application/json'
    response.raw = io.BytesIO(body.encode('utf-8'))
    return response


@pytest.mark.parametrize(
    ('status', 'body'),
    [(401, 'Invalid IAP credentials: Audience specified does not match requested endpoint'), (403, 'Forbidden')],
)
def test_iaps_refusal_is_reported_in_its_own_words(answer: list[requests.Response], status: int, body: str) -> None:
    response = _response(status, body)
    answer.append(response)
    refusal = operator_path.iap_refusal(_TARGET, 'jwt')
    assert refusal is not None
    assert f'HTTP {status}: {body}' in refusal
    assert response.request is not None
    assert response.request.url == 'https://themis.example/themis.rpc.sheaf.Sheaf/'


def test_the_probe_carries_the_jwt_alone_to_the_routed_prefix(answer: list[requests.Response]) -> None:
    response = _response(415, '')
    answer.append(response)
    assert operator_path.iap_refusal(_TARGET, 'jwt') is None
    assert response.request is not None
    assert response.request.url == 'https://themis.example/themis.rpc.sheaf.Sheaf/'
    assert response.request.headers['Proxy-Authorization'] == 'Bearer jwt'
    assert 'Authorization' not in response.request.headers
