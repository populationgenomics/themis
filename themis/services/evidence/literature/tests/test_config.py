"""The literature interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from google.api_core import exceptions as api_exceptions

from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import config
from themis.services.evidence.literature import litcache as litcache_backend

_BUCKET = 'a-bucket'


def _from_env() -> literature_backend.LiteratureBackend:
    """Select the backend as the entrypoint would; the fixture path reaches no client."""
    return config.backend_from_env(contextlib.AsyncExitStack())


class _FakeBucket:
    """A lazy bucket handle: listing a bucket that does not exist is what raises, as in GCS."""

    def __init__(self, name: str, existing: str | None) -> None:
        self._name = name
        self._existing = existing

    def list_blobs(self, *, prefix: str, max_results: int) -> list[str]:
        if self._name != self._existing:
            raise api_exceptions.NotFound(f'no such bucket: {self._name}')
        return [f'{prefix}doc-1/rendering.md'][:max_results]


class _FakeClient:
    """A ``storage.Client`` stand-in that records its own close."""

    def __init__(self, closed: list[bool], *, existing_bucket: str | None) -> None:
        self._closed = closed
        self._existing_bucket = existing_bucket

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(name, self._existing_bucket)

    def close(self) -> None:
        self._closed.append(True)


def _live_env(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_LITERATURE_FULLTEXT_BUCKET', _BUCKET)
    monkeypatch.setattr(config.storage, 'Client', lambda: client)


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_LITERATURE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_live_backend_requires_the_fulltext_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.delenv('THEMIS_LITERATURE_FULLTEXT_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_FULLTEXT_BUCKET'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_LITERATURE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_FIXTURE'):
        _from_env()


def test_fixture_selector_builds_the_seeded_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_LITERATURE_FIXTURE',
        json.dumps({'doc-1': {'title': 'A paper', 'markdown': {'gcs_uri': 'gs://c/r.md', 'from_xml': True}}}),
    )
    info = asyncio.run(_from_env().describe_paper('doc-1'))
    assert info.title == 'A paper'
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN


def test_live_selector_hands_its_client_to_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    _live_env(monkeypatch, _FakeClient(closed, existing_bucket=_BUCKET))

    async def build() -> None:
        async with contextlib.AsyncExitStack() as stack:
            assert isinstance(config.backend_from_env(stack), litcache_backend.LitcacheBackend)
            assert not closed, 'the client is held open for as long as the server runs'
        assert closed, 'the stack closed the client on unwind'

    asyncio.run(build())


def test_live_selector_fails_loud_on_an_unreadable_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    _live_env(monkeypatch, _FakeClient(closed, existing_bucket=None))

    async def build() -> None:
        async with contextlib.AsyncExitStack() as stack:
            with pytest.raises(SystemExit, match=_BUCKET):
                config.backend_from_env(stack)
        assert closed, 'a failed startup probe leaks no client'

    asyncio.run(build())
