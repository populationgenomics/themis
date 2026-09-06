"""The session-token tool's discovery of the signing key: one MAC key on the ring, version pinned."""

from __future__ import annotations

import subprocess

import pytest

from tools import session_token


def _gcloud_listing(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=['gcloud'], returncode=returncode, stdout=stdout, stderr='denied')


def test_one_mac_key_names_its_pinned_version(monkeypatch: pytest.MonkeyPatch) -> None:
    key = 'projects/p/locations/r/keyRings/themis/cryptoKeys/themis-session-token-signing-key-abc'
    monkeypatch.setattr(subprocess, 'run', lambda *_args, **_kwargs: _gcloud_listing(f'{key}\n'))
    monkeypatch.setattr(session_token.clu, 'resolve_binary', lambda name: f'/bin/{name}')

    assert session_token.signing_key_version('p') == f'{key}/cryptoKeyVersions/1'


@pytest.mark.parametrize('stdout', ['', 'one\ntwo\n'])
def test_anything_but_one_mac_key_is_refused(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(subprocess, 'run', lambda *_args, **_kwargs: _gcloud_listing(stdout))
    monkeypatch.setattr(session_token.clu, 'resolve_binary', lambda name: f'/bin/{name}')
    with pytest.raises(SystemExit, match='--key-version'):
        session_token.signing_key_version('p')


def test_a_gcloud_failure_is_reported_in_its_own_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'run', lambda *_args, **_kwargs: _gcloud_listing('', returncode=1))
    monkeypatch.setattr(session_token.clu, 'resolve_binary', lambda name: f'/bin/{name}')
    with pytest.raises(SystemExit, match='denied'):
        session_token.signing_key_version('p')
