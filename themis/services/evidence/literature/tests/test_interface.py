"""The seam: registering the interface serves the ``Literature`` rpcs over the env-selected backend."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json

import pytest

from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import interface
from themis.testing import in_process_grpc


def test_register_serves_the_literature_rpcs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_LITERATURE_FIXTURE', json.dumps({'doc-1': {'title': 'A paper'}}))
    # The fixture adapter holds no client, so it registers nothing on the stack to unwind.
    register = functools.partial(interface.register, stack=contextlib.AsyncExitStack())

    async def describe() -> literature_pb2.PaperInfo:
        async with in_process_grpc.serving(register) as channel:
            stub = literature_pb2_grpc.LiteratureStub(channel)
            return await stub.DescribePaper(literature_pb2.DescribePaperRequest(doc_id='doc-1'))

    assert asyncio.run(describe()).title == 'A paper'
