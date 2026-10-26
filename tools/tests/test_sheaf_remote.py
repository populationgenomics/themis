"""The laptop remote's bearer keeping: when tokens are minted, that the file keeps no stale one, and IAP refusals."""

from __future__ import annotations

import base64
import json
import pathlib
import time
from typing import override

import pytest

from themis.clients.sheaf import store as remote_mod
from themis.rpc import sandbox_options_pb2
from themis.sheaf import errors
from themis.sheaf import store as store_mod
from tools import clu, operator_path, sheaf_remote

_ACCOUNT = 'clu@example.iam.gserviceaccount.com'
_TARGET = operator_path.Target(
    origin='https://themis.example', service_url='https://sheaf.example', grpc_service=sheaf_remote.GRPC_SERVICE
)


def _jwt(exp: float, aud: str = 'https://sheaf.example') -> str:
    payload = base64.urlsafe_b64encode(json.dumps({'aud': aud, 'exp': exp}).encode()).decode().rstrip('=')
    return f'eyJhbGciOiJSUzI1NiJ9.{payload}.signature'


class _Minted:
    """What the stand-in IAM APIs minted, in order: ID tokens and IAP JWTs."""

    def __init__(self) -> None:
        self.bearers: list[str] = []
        self.proxy_bearers: list[str] = []


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> _Minted:
    """Stand in for the IAM Credentials API: each mint is a fresh hour-long token, recorded."""
    record = _Minted()

    def identity_token(service_account: str, audience: str) -> str:
        assert service_account == _ACCOUNT
        assert audience == _TARGET.service_url
        record.bearers.append(_jwt(time.time() + 3600))
        return record.bearers[-1]

    class IapCredentials:
        token: str | None = None

        def refresh(self, request: object) -> None:
            del request
            record.proxy_bearers.append(_jwt(time.time() + 3600, aud=f'{_TARGET.origin}/*'))
            self.token = record.proxy_bearers[-1]

    def iap_jwt_credentials(service_account: str, origin: str) -> IapCredentials:
        assert (service_account, origin) == (_ACCOUNT, _TARGET.origin)
        return IapCredentials()

    monkeypatch.setattr(clu, 'identity_token', identity_token)
    monkeypatch.setattr(clu, 'iap_jwt_credentials', iap_jwt_credentials)
    return record


def _credentials(bearer: str | None = None, proxy_bearer: str | None = None) -> remote_mod.Credentials:
    """The developer's token file: their own session, called as themself."""
    return remote_mod.Credentials(
        session_token='s', bearer=bearer, calling_as=sandbox_options_pb2.CALLING_AS_SELF, proxy_bearer=proxy_bearer
    )


def _keeper(token_file: pathlib.Path) -> sheaf_remote.BearerKeeper:
    return sheaf_remote.BearerKeeper(token_file, operator_path.Tokens(_TARGET, _ACCOUNT))


def test_both_bearers_are_minted_when_absent_and_kept_while_fresh(tmp_path: pathlib.Path, minted: _Minted) -> None:
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, _credentials())
    keeper = _keeper(token_file)

    keeper.refresh()
    keeper.refresh()

    assert (len(minted.bearers), len(minted.proxy_bearers)) == (1, 1)
    assert remote_mod.read_credentials(token_file) == _credentials(minted.bearers[0], minted.proxy_bearers[0])


def test_a_bearer_near_expiry_is_replaced(tmp_path: pathlib.Path, minted: _Minted) -> None:
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, _credentials(bearer=_jwt(time.time() + 60)))

    _keeper(token_file).refresh()

    assert minted.bearers, 'a token about to expire is minted anew'
    assert remote_mod.read_credentials(token_file).bearer == minted.bearers[0]


def test_forgetting_leaves_the_session_token_and_nothing_else(tmp_path: pathlib.Path, minted: _Minted) -> None:
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, _credentials())
    keeper = _keeper(token_file)
    keeper.refresh()

    keeper.forget()

    assert minted.bearers
    assert remote_mod.read_credentials(token_file) == _credentials()


def test_the_refreshing_store_describes_itself_as_the_plain_remote_store(
    tmp_path: pathlib.Path, minted: _Minted
) -> None:
    """The hook rebuilds a plain `RemoteStore` from the descriptor and reads the file the keeper refreshed."""
    token_file = tmp_path / 'token.json'
    fresh = time.time() + 3600
    remote_mod.write_credentials(token_file, _credentials(_jwt(fresh), _jwt(fresh)))
    with remote_mod.RemoteStore(_TARGET.origin, token_file, repo='ana') as remote:
        store = sheaf_remote.RefreshingStore(remote, _keeper(token_file))
        assert store.descriptor() == remote.descriptor()
        assert store.repo == 'ana'
    assert not minted.bearers, 'describing the store mints nothing'


class _FaultingStore(remote_mod.RemoteStore):
    """A remote store whose every read fails as grpc-python reports a stream IAP cut off."""

    @override
    def read(self) -> store_mod.Snapshot:
        raise errors.ServiceFault('UNKNOWN', 'Stream removed')


@pytest.mark.parametrize(
    ('refusal', 'expected'),
    [
        ('IAP in front of themis.rpc.sheaf.Sheaf answered HTTP 403: no access', 'answered HTTP 403: no access'),
        (None, r'Stream removed \(IAP lets the account through'),
    ],
)
def test_an_unknown_fault_names_what_iap_says(
    tmp_path: pathlib.Path,
    minted: _Minted,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str | None,
    expected: str,
) -> None:
    asked: list[str] = []

    def iap_refusal(target: operator_path.Target, proxy_bearer: str) -> str | None:
        assert target == _TARGET
        asked.append(proxy_bearer)
        return refusal

    monkeypatch.setattr(operator_path, 'iap_refusal', iap_refusal)
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, _credentials())
    keeper = _keeper(token_file)
    keeper.refresh()
    with _FaultingStore(_TARGET.origin, token_file, repo='ana') as remote:
        store = sheaf_remote.RefreshingStore(remote, keeper)
        with pytest.raises(errors.ServiceFault, match=expected):
            store.read()
    assert asked == minted.proxy_bearers, 'IAP is asked about the JWT the call carried'


def _serve_then_fail(token_file: pathlib.Path, root: pathlib.Path, seen: list[remote_mod.Credentials]) -> None:
    """Enter `serving`, record the file's credentials inside the block, and fail as a clone would."""
    with sheaf_remote.serving(
        analysis_id='ana', token_file=token_file, target=_TARGET, service_account=_ACCOUNT, port=0, root=root
    ) as instance:
        assert instance.url('ana').startswith('http://127.0.0.1:')
        seen.append(remote_mod.read_credentials(token_file))
        raise RuntimeError('the clone failed')


def test_serving_mints_both_bearers_and_removes_them_however_the_block_ends(
    tmp_path: pathlib.Path, minted: _Minted
) -> None:
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, _credentials())
    seen: list[remote_mod.Credentials] = []

    with pytest.raises(RuntimeError, match='the clone failed'):
        _serve_then_fail(token_file, tmp_path / 'mirror', seen)

    assert seen == [_credentials(minted.bearers[0], minted.proxy_bearers[0])], (
        'the block saw the tokens minted on entry'
    )
    assert remote_mod.read_credentials(token_file) == _credentials()
