"""The operator's path through IAP: what each call carries, and what an IAP refusal is reported as."""

from __future__ import annotations

import base64
import io
import json
import time

import pytest
import requests
import requests.adapters

from tools import clu, operator_path

_ACCOUNT = 'clu@example.iam.gserviceaccount.com'
_TARGET = operator_path.Target(
    origin='https://themis.example', service_url='https://sheaf.example', grpc_service='themis.rpc.sheaf.Sheaf'
)


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'eyJhbGciOiJSUzI1NiJ9.{payload}.signature'


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


def test_every_call_carries_the_service_token_and_the_iap_token(monkeypatch: pytest.MonkeyPatch) -> None:
    bearer, proxy_bearer = _jwt(time.time() + 3600), _jwt(time.time() + 3600)

    class IapCredentials:
        token = proxy_bearer

        def refresh(self, request: object) -> None:
            del request

    monkeypatch.setattr(clu, 'identity_token', lambda *_: bearer)
    monkeypatch.setattr(clu, 'iap_jwt_credentials', lambda *_: IapCredentials())
    metadata = operator_path.Tokens(_TARGET, _ACCOUNT).metadata()
    assert metadata == (('authorization', f'Bearer {bearer}'), ('proxy-authorization', f'Bearer {proxy_bearer}'))


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
    response.raw = io.BytesIO(body.encode())
    return response


@pytest.mark.parametrize(
    ('status', 'body'),
    [(401, 'Invalid IAP credentials: Audience specified does not match requested endpoint'), (403, 'Forbidden')],
)
def test_iaps_refusal_is_reported_in_its_own_words(answer: list[requests.Response], status: int, body: str) -> None:
    answer.append(_response(status, body))
    refusal = operator_path.iap_refusal(_TARGET, 'jwt')
    assert refusal is not None
    assert f'HTTP {status}: {body}' in refusal


def test_the_probe_carries_the_jwt_alone_to_the_routed_prefix(answer: list[requests.Response]) -> None:
    response = _response(415, '')
    answer.append(response)
    assert operator_path.iap_refusal(_TARGET, 'jwt') is None
    assert response.request.url == 'https://themis.example/themis.rpc.sheaf.Sheaf/'
    assert response.request.headers['Proxy-Authorization'] == 'Bearer jwt'
    assert 'Authorization' not in response.request.headers
