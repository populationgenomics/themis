"""Behaviour tests for the in-memory storage fixture and the shared version logic."""

from __future__ import annotations

import asyncio

from themis.services.store import storage as storage_mod


def test_working_document_versions_are_append_only() -> None:
    store = storage_mod.FixtureStorage()

    async def run() -> storage_mod.WorkingDocument | None:
        assert await store.put_working_document('ana', 'first') == 1
        assert await store.put_working_document('ana', 'second') == 2
        return await store.get_working_document('ana')

    latest = asyncio.run(run())
    assert latest == storage_mod.WorkingDocument(version=2, markdown='second')


def test_get_working_document_is_none_when_absent() -> None:
    store = storage_mod.FixtureStorage()
    assert asyncio.run(store.get_working_document('ana')) is None


def test_workspace_is_overwrite_on_put() -> None:
    store = storage_mod.FixtureStorage()

    async def run() -> bytes | None:
        await store.put_workspace('ana', b'first')
        await store.put_workspace('ana', b'second')
        return await store.get_workspace('ana')

    assert asyncio.run(run()) == b'second'


def test_get_workspace_is_none_when_absent() -> None:
    store = storage_mod.FixtureStorage()
    assert asyncio.run(store.get_workspace('ana')) is None


def test_next_version_succeeds_the_highest_key() -> None:
    assert storage_mod.next_version([]) == 1
    assert storage_mod.next_version([storage_mod.version_key('ana', 1), storage_mod.version_key('ana', 2)]) == 3


def test_version_key_zero_pads_for_lexical_order() -> None:
    assert storage_mod.version_key('ana', 2) < storage_mod.version_key('ana', 10)
