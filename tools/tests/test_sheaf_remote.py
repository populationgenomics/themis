"""The laptop remote's bearer keeping: when a token is minted, and that the file never keeps a stale one."""

from __future__ import annotations

import base64
import json
import pathlib
import time

import pytest

from themis.clients.sheaf import store as remote_mod
from tools import clu, sheaf_remote


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({'aud': 'https://sheaf', 'exp': exp}).encode()).decode().rstrip('=')
    return f'eyJhbGciOiJSUzI1NiJ9.{payload}.signature'


def test_jwt_expiry_reads_the_exp_claim() -> None:
    assert sheaf_remote.jwt_expiry(_jwt(1_800_000_000)) == 1_800_000_000


@pytest.mark.parametrize('token', ['not-a-jwt', 'a.b', 'a.!!!.c', f'a.{base64.urlsafe_b64encode(b"{}").decode()}.c'])
def test_jwt_expiry_refuses_what_is_not_an_id_token(token: str) -> None:
    with pytest.raises(ValueError, match='not a'):
        sheaf_remote.jwt_expiry(token)


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for gcloud: each mint is a fresh hour-long token, and the list records them."""
    tokens: list[str] = []

    def mint(service_account: str, audience: str) -> str:
        assert service_account == 'clu@example.iam.gserviceaccount.com'
        assert audience == 'https://sheaf.example'
        tokens.append(_jwt(time.time() + 3600))
        return tokens[-1]

    monkeypatch.setattr(clu, 'identity_token', mint)
    return tokens


def _keeper(token_file: pathlib.Path) -> sheaf_remote.BearerKeeper:
    return sheaf_remote.BearerKeeper(
        token_file, service_account='clu@example.iam.gserviceaccount.com', audience='https://sheaf.example'
    )


def test_a_bearer_is_minted_when_absent_and_kept_while_fresh(tmp_path: pathlib.Path, minted: list[str]) -> None:
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, remote_mod.Credentials(session_token='s', bearer=None))
    keeper = _keeper(token_file)

    keeper.refresh()
    keeper.refresh()

    assert len(minted) == 1
    assert remote_mod.read_credentials(token_file) == remote_mod.Credentials(session_token='s', bearer=minted[0])


def test_a_bearer_near_expiry_is_replaced(tmp_path: pathlib.Path, minted: list[str]) -> None:
    token_file = tmp_path / 'token.json'
    stale = _jwt(time.time() + 60)
    remote_mod.write_credentials(token_file, remote_mod.Credentials(session_token='s', bearer=stale))

    _keeper(token_file).refresh()

    assert minted, 'a token about to expire is minted anew'
    assert remote_mod.read_credentials(token_file).bearer == minted[0]


def test_forgetting_leaves_the_session_token_and_nothing_else(tmp_path: pathlib.Path, minted: list[str]) -> None:
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, remote_mod.Credentials(session_token='s', bearer=None))
    keeper = _keeper(token_file)
    keeper.refresh()

    keeper.forget()

    assert minted
    assert remote_mod.read_credentials(token_file) == remote_mod.Credentials(session_token='s', bearer=None)


def test_the_refreshing_store_describes_itself_as_the_plain_remote_store(
    tmp_path: pathlib.Path, minted: list[str]
) -> None:
    """The hook rebuilds a plain `RemoteStore` from the descriptor and reads the file the keeper refreshed."""
    token_file = tmp_path / 'token.json'
    remote_mod.write_credentials(token_file, remote_mod.Credentials(session_token='s', bearer=_jwt(time.time() + 3600)))
    with remote_mod.RemoteStore('https://sheaf.example', token_file, repo='ana') as remote:
        store = sheaf_remote.RefreshingStore(remote, _keeper(token_file))
        assert store.descriptor() == remote.descriptor()
        assert store.repo == 'ana'
    assert not minted, 'describing the store mints nothing'
