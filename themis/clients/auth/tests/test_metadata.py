"""A text metadata entry reads as its value, as ``None`` when absent, and fails loudly as bytes."""

from __future__ import annotations

import typing

import grpc.aio
import pytest

from themis.clients.auth import metadata as metadata_mod


class _FakeContextImpl:
    """A ``ServicerContext`` stand-in carrying fixed invocation metadata."""

    def __init__(self, metadata: tuple[tuple[str, str | bytes], ...]) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> tuple[tuple[str, str | bytes], ...]:
        return self._metadata


def _ctx(*metadata: tuple[str, str | bytes]) -> grpc.aio.ServicerContext:
    return typing.cast('grpc.aio.ServicerContext', _FakeContextImpl(metadata))


def test_the_first_entry_for_the_key_is_its_value() -> None:
    context = _ctx(('other', 'x'), ('x-key', 'first'), ('x-key', 'second'))
    assert metadata_mod.text_value(context, 'x-key') == 'first'


def test_a_call_without_the_key_reads_as_none() -> None:
    assert metadata_mod.text_value(_ctx(('other', 'x')), 'x-key') is None


def test_a_bytes_value_under_a_text_key_is_refused() -> None:
    with pytest.raises(TypeError, match='x-key'):
        metadata_mod.text_value(_ctx(('x-key', b'raw')), 'x-key')
