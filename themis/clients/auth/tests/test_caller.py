"""The caller verifier: a verified email exactly when the forwarded ID token verifies, ``None`` otherwise."""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Awaitable

import grpc.aio
import pytest
from google.auth import exceptions as google_auth_exceptions

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


def test_cloud_run_verifier_reads_a_verified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        caller_mod.google_id_token,
        'verify_oauth2_token',
        lambda *_a, **_k: {'email': 'sa@x', 'email_verified': True},
    )
    verify = caller_mod.cloud_run_caller_verifier()
    assert _run(verify(_ctx(_bearer('good')))) == 'sa@x'


def test_cloud_run_verifier_leaves_the_audience_unconstrained(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constraining it would tie the service to its own URL for no admission gain: any account can mint
    # a token for any audience, so reach is decided by the verified account, and Cloud Run has already
    # refused a token minted for another service.
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def verify_oauth2_token(*args: object, **kwargs: object) -> dict[str, object]:
        seen.append((args[2:], kwargs))
        return {'email': 'sa@x', 'email_verified': True}

    monkeypatch.setattr(caller_mod.google_id_token, 'verify_oauth2_token', verify_oauth2_token)
    assert _run(caller_mod.cloud_run_caller_verifier()(_ctx(_bearer('good')))) == 'sa@x'
    assert seen == [((), {})], seen


def test_cloud_run_verifier_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, object]:
        raise google_auth_exceptions.GoogleAuthError('bad token')

    monkeypatch.setattr(caller_mod.google_id_token, 'verify_oauth2_token', _raise)
    verify = caller_mod.cloud_run_caller_verifier()
    assert _run(verify(_ctx(_bearer('forged')))) is None
    # An unverified email is not an identity either.
    monkeypatch.setattr(
        caller_mod.google_id_token,
        'verify_oauth2_token',
        lambda *_a, **_k: {'email': 'sa@x', 'email_verified': False},
    )
    assert _run(verify(_ctx(_bearer('good')))) is None
    # An absent bearer never reaches verification.
    assert _run(verify(_ctx())) is None


def test_a_certificate_fetch_failure_propagates_rather_than_declining(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreachable(*_a: object, **_k: object) -> dict[str, object]:
        raise google_auth_exceptions.TransportError('certs unreachable')

    monkeypatch.setattr(caller_mod.google_id_token, 'verify_oauth2_token', _unreachable)
    verify = caller_mod.cloud_run_caller_verifier()
    with pytest.raises(google_auth_exceptions.TransportError):
        _run(verify(_ctx(_bearer('good'))))


class _FakeResponse:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None, data: bytes = b'{}') -> None:
        self.status = status
        self.headers = headers or {}
        self.data = data


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
