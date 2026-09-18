"""The per-call context: bound by the interceptor, read by a handler, absent everywhere else."""

from __future__ import annotations

import asyncio

import pytest

from themis.clients.auth import context as auth_context
from themis.rpc import auth_pb2, sandbox_options_pb2

_SELF = sandbox_options_pb2.CALLING_AS_SELF

_AUTH = auth_context.AuthContext(caller='a@x.iam.gserviceaccount.com', calling_as=_SELF, session=None)


def test_current_outside_a_gated_call_fails_loud() -> None:
    with pytest.raises(LookupError, match='outside a call the auth interceptor gated'):
        auth_context.current()


def test_bind_makes_a_context_current_and_unbind_restores_the_absence() -> None:
    token = auth_context.bind(_AUTH)
    try:
        assert auth_context.current() is _AUTH
    finally:
        auth_context.unbind(token)
    with pytest.raises(LookupError):
        auth_context.current()


def test_a_binding_is_per_task() -> None:
    # Two concurrent calls each see their own context: the interceptor binds within the task that runs
    # the handler, and nothing leaks across tasks.
    seen: dict[str, str] = {}

    async def call(name: str) -> None:
        token = auth_context.bind(auth_context.AuthContext(caller=name, calling_as=_SELF, session=None))
        try:
            await asyncio.sleep(0)
            seen[name] = auth_context.current().caller
        finally:
            auth_context.unbind(token)

    async def run() -> None:
        await asyncio.gather(call('one'), call('two'))

    asyncio.run(run())
    assert seen == {'one': 'one', 'two': 'two'}


def test_the_context_is_immutable() -> None:
    auth = auth_context.AuthContext(
        caller='a@x',
        calling_as=_SELF,
        session=auth_pb2.SessionContext(project_id='p', analysis_id='a'),
    )
    with pytest.raises(AttributeError):
        auth.caller = 'b@x'  # type: ignore[misc]
