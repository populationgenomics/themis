"""The one copy of the ACL: refused at start when invalid, swapped whole, kept on failure, refused once stale."""

from __future__ import annotations

import asyncio
import logging
from typing import override

import pytest

from themis.services.auth import acl, acl_holder
from themis.services.auth.tests import harness

_LIMIT = harness.STALENESS_LIMIT_SECONDS
_REJECTED = 'acl rejected: '


def _without_project(project_id: str) -> bytes:
    projects = {pid: entry for pid, entry in harness.ACL['projects'].items() if pid != project_id}  # pyright: ignore[reportAttributeAccessIssue]
    return harness.acl_version({**harness.ACL, 'projects': projects})


async def _load(backend: harness.Backend, clock: harness.Clock) -> acl_holder.AclHolder:
    return await acl_holder.AclHolder.load(
        backend.read_acl,
        read_timeout_seconds=harness.READ_TIMEOUT_SECONDS,
        staleness_limit_seconds=_LIMIT,
        clock=clock,
    )


def _rejections(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(_REJECTED)]


@pytest.mark.parametrize(
    'content',
    [pytest.param(b'{"format": 2}', id='invalid'), pytest.param(OSError('secret unreachable'), id='unreadable')],
)
def test_the_service_refuses_to_start_without_a_valid_acl(
    content: bytes | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    backend = harness.Backend({}, b'')
    backend.acl = content
    with pytest.raises(SystemExit, match=_REJECTED):
        asyncio.run(_load(backend, harness.Clock()))
    assert [r.levelno for r in _rejections(caplog)] == [logging.ERROR]


def test_a_valid_version_is_swapped_in_whole() -> None:
    async def run() -> acl.Snapshot:
        backend = harness.Backend({}, harness.acl_version())
        holder = await _load(backend, harness.Clock())
        backend.acl = _without_project('asd')
        await holder.refresh()
        return holder.current()

    assert set(asyncio.run(run()).projects) == {'epi'}


@pytest.mark.parametrize(
    'content',
    [
        pytest.param(_without_project('asd').replace(b'"format": 1', b'"format": 9'), id='invalid'),
        pytest.param(OSError('secret unreachable'), id='unreadable'),
        pytest.param(b'[' * 100_000, id='undecodable'),
    ],
)
def test_a_failed_version_keeps_the_last_good_copy_and_is_logged_at_error(
    content: bytes | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> acl.Snapshot:
        backend = harness.Backend({}, harness.acl_version())
        holder = await _load(backend, harness.Clock())
        backend.acl = content
        await holder.refresh()
        return holder.current()

    assert set(asyncio.run(run()).projects) == {'asd', 'epi'}
    assert [r.levelno for r in _rejections(caplog)] == [logging.ERROR]


def test_the_last_good_copy_serves_until_the_limit_is_passed_and_again_once_a_version_loads() -> None:
    async def run() -> None:
        clock = harness.Clock()
        backend = harness.Backend({}, harness.acl_version())
        holder = await _load(backend, clock)
        backend.acl = OSError('secret unreachable')

        clock.advance(_LIMIT)
        await holder.refresh()
        assert holder.current().projects, 'at the limit, the stale copy still answers'

        clock.advance(1)
        with pytest.raises(acl_holder.StaleAclError):
            holder.current()

        backend.acl = _without_project('epi')
        await holder.refresh()
        assert set(holder.current().projects) == {'asd'}

    asyncio.run(run())


def test_a_refresh_of_an_unchanged_valid_version_renews_the_copy() -> None:
    async def run() -> None:
        clock = harness.Clock()
        holder = await _load(harness.Backend({}, harness.acl_version()), clock)
        clock.advance(_LIMIT)
        await holder.refresh()
        clock.advance(_LIMIT)
        holder.current()

    asyncio.run(run())


def test_the_refresher_keeps_refreshing_through_failures() -> None:
    reads: list[None] = []

    class _Counting(harness.Backend):
        @override
        async def read_acl(self) -> bytes:
            reads.append(None)
            if len(reads) == 2:
                raise OSError('a transient failure')
            return await super().read_acl()

    async def run() -> None:
        holder = await _load(_Counting({}, harness.acl_version()), harness.Clock())
        refresher = asyncio.create_task(holder.refresh_forever(0))
        while len(reads) < 4:
            assert not refresher.done(), refresher
            await asyncio.sleep(0)
        refresher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresher

    asyncio.run(run())


def test_a_read_past_the_timeout_is_abandoned_and_keeps_the_last_good_copy(caplog: pytest.LogCaptureFixture) -> None:
    hang = False

    class _Hanging(harness.Backend):
        @override
        async def read_acl(self) -> bytes:
            if hang:
                await asyncio.Event().wait()
            return await super().read_acl()

    async def run() -> acl.Snapshot:
        nonlocal hang
        holder = await acl_holder.AclHolder.load(
            _Hanging({}, harness.acl_version()).read_acl,
            read_timeout_seconds=0.01,
            staleness_limit_seconds=_LIMIT,
            clock=harness.Clock(),
        )
        hang = True
        await holder.refresh()
        return holder.current()

    assert set(asyncio.run(run()).projects) == {'asd', 'epi'}
    assert [r.levelno for r in _rejections(caplog)] == [logging.ERROR]
