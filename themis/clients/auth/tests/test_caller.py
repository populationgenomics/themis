"""The caller verifier: a verified email exactly when the forwarded ID token verifies, ``None`` otherwise."""

from __future__ import annotations

import asyncio
import datetime
import json
import time
import typing
from collections.abc import Awaitable

import grpc.aio
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt as google_crypt
from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt as google_jwt

from themis.clients.auth import caller as caller_mod


class _FakeContextImpl:
    """A ``ServicerContext`` stand-in carrying fixed invocation metadata; a verifier reads no more."""

    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


def _ctx(metadata: tuple[tuple[str, str], ...] = ()) -> grpc.aio.ServicerContext:
    return typing.cast('grpc.aio.ServicerContext', _FakeContextImpl(metadata))


def _bearer(token: str) -> tuple[tuple[str, str], ...]:
    return (('authorization', f'Bearer {token}'),)


def _run[T](awaitable: Awaitable[T]) -> T:
    return asyncio.run(awaitable)  # pyright: ignore[reportArgumentType]


def test_fixture_verifier_maps_a_seeded_bearer_to_its_email() -> None:
    verify = caller_mod.fixture_caller_verifier_from_json('{"tok": "sa@x"}', var_name='TEST')
    assert _run(verify(_ctx(_bearer('tok')))) == 'sa@x'
    assert _run(verify(_ctx(_bearer('other')))) is None
    assert _run(verify(_ctx())) is None


@pytest.mark.parametrize('raw', [None, '{"tok": 5}', 'not json', '[]'])
def test_fixture_verifier_rejects_a_malformed_seed(raw: str | None) -> None:
    with pytest.raises(SystemExit):
        caller_mod.fixture_caller_verifier_from_json(raw, var_name='TEST')


def test_a_non_bearer_authorization_is_no_token() -> None:
    verify = caller_mod.fixture_caller_verifier_from_json('{"tok": "sa@x"}', var_name='TEST')
    assert _run(verify(_ctx((('authorization', 'Basic tok'),)))) is None
    assert _run(verify(_ctx((('authorization', 'Bearer'),)))) is None


class _FakeResponse:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, data: bytes = b'{}') -> None:
        self.status = status
        self.headers = headers or {}
        self.data = data


_AUDIENCE = 'https://themis-sheaf-123456789.australia-southeast1.run.app'
_KEY_ID = 'test-key'


@pytest.fixture(scope='module')
def google_key() -> tuple[str, str]:
    """An RSA key standing in for one of Google's ID-token signing keys: (private key PEM, certificate PEM)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, 'test')])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return private_pem, certificate.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def google_certs(monkeypatch: pytest.MonkeyPatch, google_key: tuple[str, str]) -> None:
    """Serve the stand-in key as Google's certificate set to the verifier's transport."""
    body = json.dumps({_KEY_ID: google_key[1]}).encode()

    class _Transport:
        def __call__(self, _url: str, **_k: object) -> _FakeResponse:
            return _FakeResponse(headers={'cache-control': 'max-age=600'}, data=body)

    monkeypatch.setattr(caller_mod.google_auth_requests, 'Request', _Transport)


def _id_token(google_key: tuple[str, str], *, audience: str, email_verified: bool = True) -> str:
    now = int(time.time())
    claims = {
        'iss': 'https://accounts.google.com',
        'aud': audience,
        'email': 'sa@x',
        'email_verified': email_verified,
        'iat': now,
        'exp': now + 600,
    }
    signer = google_crypt.RSASigner.from_string(google_key[0], key_id=_KEY_ID)
    return google_jwt.encode(signer, claims).decode()


@pytest.mark.usefixtures('google_certs')
def test_cloud_run_verifier_reads_the_email_of_a_token_for_its_audience(google_key: tuple[str, str]) -> None:
    verify = caller_mod.cloud_run_caller_verifier(_AUDIENCE)
    assert _run(verify(_ctx(_bearer(_id_token(google_key, audience=_AUDIENCE))))) == 'sa@x'


@pytest.mark.usefixtures('google_certs')
@pytest.mark.parametrize(
    'audience',
    [
        'https://unrelated-audience.invalid',
        # The same service's other run.app URL: Cloud Run admits it, this service does not.
        'https://themis-sheaf-abcdefghij-ts.a.run.app',
        f'{_AUDIENCE}/',
    ],
)
def test_cloud_run_verifier_declines_a_token_for_another_audience(google_key: tuple[str, str], audience: str) -> None:
    # Behind the load balancer's IAP, Cloud Run admits IAP's service agent and never looks at this token.
    verify = caller_mod.cloud_run_caller_verifier(_AUDIENCE)
    assert _run(verify(_ctx(_bearer(_id_token(google_key, audience=audience))))) is None


@pytest.mark.usefixtures('google_certs')
def test_cloud_run_verifier_declines_an_unverified_email(google_key: tuple[str, str]) -> None:
    verify = caller_mod.cloud_run_caller_verifier(_AUDIENCE)
    token = _id_token(google_key, audience=_AUDIENCE, email_verified=False)
    assert _run(verify(_ctx(_bearer(token)))) is None


@pytest.mark.parametrize('audience', ['', 'themis-sheaf', 'http://themis-sheaf.run.app'])
def test_cloud_run_verifier_takes_only_an_https_audience(audience: str) -> None:
    with pytest.raises(ValueError, match='https://'):
        caller_mod.cloud_run_caller_verifier(audience)


def test_cloud_run_verifier_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, object]:
        raise google_auth_exceptions.GoogleAuthError('bad token')

    monkeypatch.setattr(caller_mod.google_id_token, 'verify_oauth2_token', _raise)
    verify = caller_mod.cloud_run_caller_verifier(_AUDIENCE)
    assert _run(verify(_ctx(_bearer('forged')))) is None
    # An absent bearer never reaches verification.
    assert _run(verify(_ctx())) is None


def test_a_certificate_fetch_failure_propagates_rather_than_declining(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreachable(*_a: object, **_k: object) -> dict[str, object]:
        raise google_auth_exceptions.TransportError('certs unreachable')

    monkeypatch.setattr(caller_mod.google_id_token, 'verify_oauth2_token', _unreachable)
    verify = caller_mod.cloud_run_caller_verifier(_AUDIENCE)
    with pytest.raises(google_auth_exceptions.TransportError):
        _run(verify(_ctx(_bearer('good'))))


def test_the_caching_transport_fetches_once_per_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches: list[str] = []

    class _Transport:
        def __call__(self, url: str, **_k: object) -> _FakeResponse:
            fetches.append(url)
            return _FakeResponse(headers={'cache-control': 'public, max-age=600'}, data=b'{"kid": "cert"}')

    monkeypatch.setattr(caller_mod.google_auth_requests, 'Request', _Transport)
    request = caller_mod._CachingRequest()
    first = request('https://certs')
    second = request('https://certs')
    assert fetches == ['https://certs']
    assert first.data == second.data == b'{"kid": "cert"}'


def test_the_caching_transport_refetches_once_the_max_age_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches: list[str] = []
    clock = [1000.0]

    class _Transport:
        def __call__(self, url: str, **_k: object) -> _FakeResponse:
            fetches.append(url)
            return _FakeResponse(headers={'cache-control': 'max-age=60'})

    monkeypatch.setattr(caller_mod.google_auth_requests, 'Request', _Transport)
    monkeypatch.setattr(caller_mod.time, 'monotonic', lambda: clock[0])
    request = caller_mod._CachingRequest()
    request('https://certs')
    clock[0] += 61
    request('https://certs')
    assert fetches == ['https://certs', 'https://certs']


def test_the_caching_transport_does_not_cache_a_failed_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    fetches: list[str] = []

    class _Transport:
        def __call__(self, url: str, **_k: object) -> _FakeResponse:
            fetches.append(url)
            return _FakeResponse(status=503)

    monkeypatch.setattr(caller_mod.google_auth_requests, 'Request', _Transport)
    request = caller_mod._CachingRequest()
    request('https://certs')
    request('https://certs')
    assert fetches == ['https://certs', 'https://certs']
