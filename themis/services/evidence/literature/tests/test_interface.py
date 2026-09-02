"""The seam: registering the interface serves the ``Literature`` rpcs over the env-selected backend."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json

import httpx2
import pytest

from themis.rpc import auth_pb2, literature_pb2, literature_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.literature import interface
from themis.testing import in_process_grpc


def _deps() -> deps_mod.Deps:
    """Image-level collaborators, with a resolver no read here may reach."""
    return deps_mod.Deps(
        session_resolver=_unreachable_resolver, http_client=httpx2.AsyncClient(), stack=contextlib.AsyncExitStack()
    )


async def _unreachable_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('a literature read resolves no session; only the conversion enqueue does')


def _seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_LITERATURE_FIXTURE',
        json.dumps(
            {
                'store': {'doc-1': {'title': 'A paper'}},
                'discovery': {
                    'records': [{'pmid': '111', 'title': 'An indexed paper', 'abstract': 'Something.'}],
                    'entities': [],
                    'book_articles': [],
                },
            }
        ),
    )


def test_register_serves_the_store_rpcs(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch)
    # The fixture backend holds no client, so it registers nothing on the stack to unwind, and it
    # reads no session — literature is the one interface with no authorizer.
    register = functools.partial(interface.register, deps=_deps())

    async def describe() -> literature_pb2.PaperInfo:
        async with in_process_grpc.serving(register) as channel:
            stub = literature_pb2_grpc.LiteratureStub(channel)
            return await stub.DescribePaper(literature_pb2.DescribePaperRequest(doc_id='doc-1'))

    assert asyncio.run(describe()).title == 'A paper'


def test_register_serves_the_discovery_rpcs(monkeypatch: pytest.MonkeyPatch) -> None:
    # One backend spans both halves of the seed: registering has to serve the index rpcs too, not
    # only the store's.
    _seed(monkeypatch)
    register = functools.partial(interface.register, deps=_deps())

    async def search() -> literature_pb2.SearchEuropePmcResponse:
        async with in_process_grpc.serving(register) as channel:
            stub = literature_pb2_grpc.LiteratureStub(channel)
            return await stub.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='indexed'))

    assert [record.pmid for record in asyncio.run(search()).records] == ['111']
