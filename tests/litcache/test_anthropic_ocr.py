"""Tests for the PDF LLM-OCR converter (`themis.litcache.anthropic_ocr`).

The Anthropic client is faked so the transcription path runs offline: the streaming context manager
yields a canned final `Message`, exercising the `stop_reason` guards, the multi-block text join, and
the model read-back (the recorded model is the response's, not the requested one) without touching
the network.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import typing

import anthropic
import pytest

from themis.litcache import anthropic_ocr, ocr

_PDF_BYTES = b'%PDF-1.7 body'


@dataclasses.dataclass(frozen=True)
class _Block:
    type: str
    text: str = ''


@dataclasses.dataclass(frozen=True)
class _Message:
    stop_reason: str
    content: list[_Block]
    model: str


class _FakeStream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def get_final_message(self) -> _Message:
        return self._message


class _FakeMessages:
    def __init__(self, message: _Message, captured: dict[str, object]) -> None:
        self._message = message
        self._captured = captured

    def stream(self, **kwargs: object) -> _FakeStream:
        self._captured.update(kwargs)
        return _FakeStream(self._message)


class _FakeClient:
    def __init__(self, message: _Message, captured: dict[str, object]) -> None:
        self.messages = _FakeMessages(message, captured)

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _install(monkeypatch: pytest.MonkeyPatch, message: _Message) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', lambda **_kw: _FakeClient(message, captured))
    return captured


class _StalledMessages:
    """A stream that never yields, standing in for a call that outlives its elapsed budget."""

    def stream(self, **_kw: object) -> typing.Self:
        return self

    async def __aenter__(self) -> typing.Self:
        await asyncio.sleep(3600)
        raise AssertionError('unreachable: the elapsed bound should have fired')

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _StalledClient:
    def __init__(self) -> None:
        self.messages = _StalledMessages()

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def test_a_silence_is_caught_before_the_elapsed_budget_runs_out() -> None:
    # The two bounds have to differ. asyncio.timeout is entered before the client connects, so an idle
    # gap as long as the elapsed budget can never elapse inside it — given one value the stall bound is
    # unreachable and a stream that dies early settles the paper terminally instead of being retried.
    assert anthropic_ocr._STALL_SECONDS < anthropic_ocr._TIMEOUT_SECONDS


def test_a_call_past_the_elapsed_bound_is_terminal_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # OcrError, not TimeoutError: the bound is the most the queue's dispatch deadline allows, so the
    # producer settles the paper FAILED rather than re-billing the model on every retry. httpx has no
    # total-duration timeout, so the client's own value would not have caught a call that keeps
    # streaming — only asyncio.timeout bounds elapsed time.
    monkeypatch.setattr(anthropic, 'AsyncAnthropic', lambda **_kw: _StalledClient())
    monkeypatch.setattr(anthropic_ocr, '_TIMEOUT_SECONDS', 0.01)

    with pytest.raises(ocr.OcrError, match='exceeded'):
        asyncio.run(anthropic_ocr.convert_pdf(_PDF_BYTES))


def test_convert_pdf_joins_text_blocks_and_reads_back_the_response_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-text block is skipped; the response's own model id is recorded, not the requested one.
    message = _Message(
        stop_reason='end_turn',
        content=[_Block('text', '# Title\n'), _Block('thinking', 'ignored'), _Block('text', 'Body.')],
        model='claude-sonnet-5-20260101',
    )
    captured = _install(monkeypatch, message)

    result = asyncio.run(anthropic_ocr.convert_pdf(_PDF_BYTES))

    assert result.markdown == '# Title\nBody.'
    assert result.model == 'claude-sonnet-5-20260101'
    # The shared prompt version leads, this provider's own request shape follows — a prompt change
    # supersedes every provider's renderings, a request-shape change only this one's.
    assert result.converter_version == f'{ocr.PROMPT_VERSION}.{anthropic_ocr.HARNESS_VERSION}'
    # The PDF bytes are actually sent for transcription (base64-encoded in the request).
    sent = json.dumps(captured['messages'])
    assert base64.standard_b64encode(_PDF_BYTES).decode('ascii') in sent
    # Against the shared instruction, not one of its own: holding the prompt fixed across providers is
    # what makes a transcription difference attributable to the model.
    blocks = json.loads(sent)[0]['content']
    assert [b['text'] for b in blocks if b['type'] == 'text'] == [ocr.INSTRUCTION]


def test_convert_pdf_raises_when_truncated_at_the_token_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _Message(stop_reason='max_tokens', content=[_Block('text', 'partial')], model='claude-sonnet-5')
    _install(monkeypatch, message)
    with pytest.raises(ocr.OcrError, match='truncated'):
        asyncio.run(anthropic_ocr.convert_pdf(_PDF_BYTES))


def test_convert_pdf_raises_when_the_model_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _Message(stop_reason='refusal', content=[], model='claude-sonnet-5')
    _install(monkeypatch, message)
    with pytest.raises(ocr.OcrError, match='refused'):
        asyncio.run(anthropic_ocr.convert_pdf(_PDF_BYTES))
