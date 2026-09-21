"""The developer identity's token carries what a gated service verifies the caller from."""

from __future__ import annotations

import subprocess

import pytest

from tools import clu


def _recording_gcloud(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    """Stand in for `gcloud`: record the argv, answer `stdout`."""
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr='')

    monkeypatch.setattr(clu.subprocess, 'run', run)
    monkeypatch.setattr(clu, 'resolve_binary', lambda name: f'/bin/{name}')
    return calls


def test_the_identity_token_is_minted_with_the_email_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recording_gcloud(monkeypatch, 'tok\n')
    assert clu.identity_token('clu@x.iam.gserviceaccount.com', 'https://svc.example') == 'tok'
    (argv,) = calls
    assert '--include-email' in argv
    assert '--impersonate-service-account=clu@x.iam.gserviceaccount.com' in argv
    assert '--audiences=https://svc.example' in argv


def test_an_empty_token_is_a_gcloud_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _recording_gcloud(monkeypatch, '')
    with pytest.raises(clu.GcloudError, match='no identity token'):
        clu.identity_token('clu@x.iam.gserviceaccount.com', 'https://svc.example')
