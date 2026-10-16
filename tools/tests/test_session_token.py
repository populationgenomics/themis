"""The session-token tool's discovery of the signing key: one MAC key on the ring, version pinned."""

from __future__ import annotations

import types
from collections.abc import Sequence
from typing import Self, override

import pytest
from google.api_core import exceptions as api_exceptions
from google.auth import credentials as credentials_mod
from google.auth import exceptions

from tools import session_token


class _Login(credentials_mod.Credentials):
    """The caller's application-default login: its refresh succeeds, or fails with `error`."""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self._error = error

    @override
    def refresh(self, request: object) -> None:
        del request
        if self._error is not None:
            raise self._error
        self.token = 'access-token'  # noqa: S105 — a stand-in, not a secret


def _key_ring(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keys: Sequence[str] = (),
    error: Exception | None = None,
    login: _Login | None = None,
) -> None:
    """Stand in for Cloud KMS: the ring's MAC keys are `keys`, or listing it fails with `error`."""

    class KeyManagementServiceClient:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc_info: object) -> None:
            del exc_info

        def list_crypto_keys(self, request: dict[str, str]) -> list[types.SimpleNamespace]:
            del request
            if error is not None:
                raise error
            return [types.SimpleNamespace(name=key) for key in keys]

    def caller_credentials(*, quota_project: str | None) -> _Login:
        del quota_project
        return login or _Login()

    monkeypatch.setattr(session_token.clu, 'caller_credentials', caller_credentials)
    monkeypatch.setattr(session_token.kms_v1, 'KeyManagementServiceClient', KeyManagementServiceClient)


def test_one_mac_key_names_its_pinned_version(monkeypatch: pytest.MonkeyPatch) -> None:
    key = 'projects/p/locations/r/keyRings/themis/cryptoKeys/themis-session-token-signing-key-abc'
    _key_ring(monkeypatch, keys=[key])
    assert session_token.signing_key_version('p') == f'{key}/cryptoKeyVersions/1'


@pytest.mark.parametrize('keys', [[], ['one', 'two']])
def test_anything_but_one_mac_key_is_refused(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> None:
    _key_ring(monkeypatch, keys=keys)
    with pytest.raises(session_token.TokenError, match='--key-version'):
        session_token.signing_key_version('p')


@pytest.mark.parametrize(
    'error',
    [
        api_exceptions.PermissionDenied('cloudkms.cryptoKeys.list denied'),
        api_exceptions.RetryError('cloudkms.cryptoKeys.list denied', cause=None),
    ],
)
def test_a_kms_failure_is_reported_in_its_own_words(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    _key_ring(monkeypatch, error=error)
    with pytest.raises(session_token.clu.GoogleCallError, match=r'cloudkms\.cryptoKeys\.list denied'):
        session_token.signing_key_version('p')


def test_a_login_that_cannot_refresh_is_reported_in_its_own_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _key_ring(monkeypatch, login=_Login(exceptions.RefreshError('Reauthentication is needed')))
    with pytest.raises(session_token.clu.GoogleCallError, match='Reauthentication is needed'):
        session_token.signing_key_version('p')
