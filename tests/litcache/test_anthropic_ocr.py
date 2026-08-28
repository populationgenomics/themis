"""Tests for the PDF LLM-OCR converter (`themis.litcache.anthropic_ocr`).

The Anthropic client is faked so the transcription path runs offline: the streaming context manager
yields a canned final `Message`, exercising the `stop_reason` guards, the multi-block text join, and
the model read-back (the recorded model is the response's, not the requested one) without touching
the network. Pages are rasterized for real, because the size a page is rendered at is the size Claude
reads it at, and a fake that skipped it would let a sizing bug through.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import io
import json
import logging
import pathlib
import typing
from collections.abc import Iterator

import anthropic
import pypdfium2
import pytest
from anthropic.lib import credentials as anthropic_credentials

from themis.litcache import anthropic_ocr, claude_images, ocr, pdf

# A real paper, whose pages carry figures and dense two-column type: a blank page encodes to almost
# nothing, so only this says anything about what a request actually weighs.
_PAPER = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'oa' / 'source.pdf'

# The request body the API accepts, which the base64-encoded page images have to fit inside.
_MAX_REQUEST_BYTES = 32 * 1024 * 1024


def _pdf(pages: int = 2) -> bytes:
    """A pdf with `pages` blank US-letter pages."""
    document = pypdfium2.PdfDocument.new()
    for _ in range(pages):
        document.new_page(612, 792)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paper_of(pages: int) -> bytes:
    """The fixture paper's pages, repeated until the document is `pages` long."""
    document = pypdfium2.PdfDocument.new()
    while len(document) < pages:
        with pypdfium2.PdfDocument(_PAPER.read_bytes()) as source:
            document.import_pages(source, list(range(min(len(source), pages - len(document)))))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@dataclasses.dataclass(frozen=True)
class _Block:
    type: str
    text: str = ''


@dataclasses.dataclass(frozen=True)
class _Usage:
    input_tokens: int = 4321
    output_tokens: int = 8765
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclasses.dataclass(frozen=True)
class _Message:
    stop_reason: str
    content: list[_Block]
    model: str
    usage: _Usage = dataclasses.field(default_factory=_Usage)


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
    """Fake the client; the returned dict collects both its constructor and its stream kwargs."""
    captured: dict[str, object] = {}

    def client(**kwargs: object) -> _FakeClient:
        captured.update(kwargs)
        return _FakeClient(message, captured)

    monkeypatch.setattr(anthropic, 'AsyncAnthropic', client)
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
        asyncio.run(anthropic_ocr.convert_pdf(_pdf(1)))


def _sent_blocks(captured: dict[str, object]) -> list[dict[str, typing.Any]]:
    """The content blocks of the one user turn the converter sent."""
    return json.loads(json.dumps(captured['messages']))[0]['content']


@contextlib.contextmanager
def _budget(page_bytes: int) -> Iterator[None]:
    """Run with the encoded-page budget one request has set to `page_bytes`."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(anthropic_ocr, '_PAGE_BYTE_BUDGET', page_bytes)
        yield


# --- how pages are sized ---


def test_the_whole_page_image_fits_the_tier_claude_reads_at() -> None:
    # Sized the other way round the image is resized on arrival and the transcription is made from
    # something we never saw.
    (page,) = anthropic_ocr.render(_pdf(1))
    image = (page.width, page.height)

    assert (
        claude_images.resized_size(
            *image,
            max_edge=claude_images.HIGH_RESOLUTION.max_edge,
            max_tokens=claude_images.HIGH_RESOLUTION.max_tokens,
        )
        == image
    )


def test_a_long_paper_stays_under_the_many_image_edge_limit() -> None:
    # Past twenty images the API rejects the whole request unless every image is under this, so the
    # cap is a precondition of the request succeeding at all, not a cost choice.
    pages = anthropic_ocr.render(_pdf(claude_images.MANY_IMAGE_THRESHOLD + 2))

    assert len(pages) > claude_images.MANY_IMAGE_THRESHOLD
    assert all(max(page.width, page.height) <= claude_images.MANY_IMAGE_MAX_EDGE for page in pages)


def test_a_paper_whose_pages_together_overrun_is_rendered_small_enough_to_send() -> None:
    # What one page weighs is not what twenty weigh, and the API rejects the whole request rather
    # than trimming it. The budget is forced because the fixture no longer overruns on its own: at
    # twenty pages it encodes to ~25 MB of the 32 MB a request carries, and past twenty the
    # many-image edge cap shrinks the pages before their weight ever does.
    (alone,) = anthropic_ocr.render(_paper_of(1))
    twenty = anthropic_ocr.render(_paper_of(20))
    assert sum(len(page.png) for page in twenty) * 4 / 3 < _MAX_REQUEST_BYTES

    with _budget(sum(len(page.png) for page in twenty) - 1):
        pages = anthropic_ocr.render(_paper_of(20))

    assert len(pages) == 20
    assert pages[0].width < alone.width


def test_a_page_too_heavy_to_send_is_rendered_smaller_before_it_is_given_up_on() -> None:
    # A budget just under one page's own weight drives the loop's body, not only its exit. A real
    # page, not a blank one: blank pages encode to almost nothing at any size, so shrinking one
    # frees no bytes and the loop has nothing to show.
    (full,) = anthropic_ocr.render(_paper_of(1))

    with _budget(len(full.png) - 1):
        (shrunk,) = anthropic_ocr.render(_paper_of(1))

    assert shrunk.width < full.width


def test_an_unloadable_pdf_is_terminal_not_retried() -> None:
    # The same bytes will not load next time. Left as a PdfiumError it is a 5xx, five Cloud Tasks
    # retries, and a paper stuck PENDING with nothing recording why: produce.py settles OcrError alone.
    with pytest.raises(ocr.OcrError, match='could not be loaded'):
        anthropic_ocr.render(b'not a pdf')


def _raise(error: Exception) -> object:
    """A stand-in that fails the way the real call would."""

    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    return fail


@pytest.mark.parametrize(
    'error',
    [ValueError('page 3 is 0x0 points'), pypdfium2.PdfiumError('Failed to load page.')],
    ids=['degenerate-size', 'page-will-not-load'],
)
def test_a_page_that_will_not_rasterize_is_terminal_not_retried(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    # A page the document's tree offers but pdfium will not render: a degenerate size, or
    # FPDF_LoadPage returning NULL. Both permanent; untranslated either is a 5xx and five retries
    # that render the same page again. Driven through the seam because pdfium normalises every
    # degenerate size reachable from here.
    monkeypatch.setattr(pdf, 'render_pages', _raise(error))

    with pytest.raises(ocr.OcrError, match='unrenderable page'):
        anthropic_ocr.render(_paper_of(1))


def test_the_shrink_makes_progress_when_the_token_budget_binds() -> None:
    # _page_fit solves against the tier's token budget as well as its edge. On US letter the budget
    # binds, so scaling the previous cap fits to the same pixels and the loop reads "unchanged" as
    # "nothing left to try" — settling a paper that a smaller render would have sent.
    letter = _pdf(1)  # 612x792, the ratio at which the token budget binds before the edge does
    (full,) = anthropic_ocr.render(letter)

    with _budget(len(full.png) - 1):
        (shrunk,) = anthropic_ocr.render(letter)

    assert shrunk.height < full.height


def test_a_paper_that_will_not_fit_a_request_is_terminal() -> None:
    # Shrinking stops at the standard tier's edge rather than rendering something illegible, and a
    # paper still too large there will not transcribe however many times it is asked.
    with _budget(1), pytest.raises(ocr.OcrError, match='one request'):
        anthropic_ocr.render(_pdf(1))


def test_a_paper_of_more_pages_than_a_request_carries_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Image blocks are capped per request, and no amount of shrinking removes a page.
    monkeypatch.setattr(claude_images, 'MAX_IMAGE_BLOCKS', 2)

    with pytest.raises(ocr.OcrError, match='image blocks'):
        anthropic_ocr.render(_pdf(3))


@pytest.mark.parametrize(
    ('pages', 'capped'),
    [(claude_images.MANY_IMAGE_THRESHOLD, False), (claude_images.MANY_IMAGE_THRESHOLD + 1, True)],
)
def test_the_many_image_cap_turns_on_above_the_threshold_and_not_at_it(pages: int, capped: bool) -> None:
    # "More than twenty" is an off-by-one waiting to happen, and getting it wrong either wastes
    # resolution on every long paper or has the API reject it outright.
    rendered = anthropic_ocr.render(_pdf(pages))

    assert all(max(page.width, page.height) <= claude_images.MANY_IMAGE_MAX_EDGE for page in rendered) == capped


def test_a_short_paper_is_rendered_larger_than_that_limit_allows() -> None:
    # The cap is not applied when it does not have to be: fidelity is the thing being bought.
    (page,) = anthropic_ocr.render(_pdf(1))

    assert max(page.width, page.height) > claude_images.MANY_IMAGE_MAX_EDGE


# --- the request ---


def test_the_supplied_credential_is_the_one_the_client_authenticates_with(monkeypatch: pytest.MonkeyPatch) -> None:
    # The credential has to reach the client, and be built per call: an explicit one is total (the
    # client then reads no credential env var), and the client closes the provider it is handed.
    built: list[anthropic_credentials.AccessTokenProvider] = []

    def token(*, force_refresh: bool = False) -> anthropic_credentials.AccessToken:
        del force_refresh
        raise AssertionError('unreachable: the faked client never authenticates')

    def credentials() -> anthropic_credentials.AccessTokenProvider:
        built.append(token)
        return token

    captured = _install(monkeypatch, _Message(stop_reason='end_turn', content=[_Block('text', '#')], model='m'))

    asyncio.run(anthropic_ocr.convert_pdf(_pdf(1), credentials=credentials))
    asyncio.run(anthropic_ocr.convert_pdf(_pdf(1), credentials=credentials))

    assert captured['credentials'] is token
    assert len(built) == 2  # built per call, not shared


def test_no_factory_leaves_the_client_without_an_explicit_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit credential is what makes the SDK consult the environment; anything else would pin
    # the call to a provider the caller never chose.
    captured = _install(monkeypatch, _Message(stop_reason='end_turn', content=[_Block('text', '#')], model='m'))

    asyncio.run(anthropic_ocr.convert_pdf(_pdf(1)))

    assert captured['credentials'] is None


def test_convert_pdf_joins_text_blocks_and_reads_back_the_response_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-text block is skipped; the response's own model id is recorded, not the requested one.
    message = _Message(
        stop_reason='end_turn',
        content=[_Block('text', '# Title\n'), _Block('thinking', 'ignored'), _Block('text', 'Body.')],
        model='claude-sonnet-5-20260101',
    )
    captured = _install(monkeypatch, message)

    result = asyncio.run(anthropic_ocr.convert_pdf(_pdf()))

    assert result.markdown == '# Title\nBody.'
    assert result.model == 'claude-sonnet-5-20260101'
    # The shared prompt version leads, this provider's own request shape follows — a prompt change
    # supersedes every provider's renderings, a request-shape change only this one's.
    assert result.converter_version == f'{ocr.PROMPT_VERSION}.{anthropic_ocr.HARNESS_VERSION}'
    # Against the shared instruction, not one of its own: holding the prompt fixed across providers is
    # what makes a transcription difference attributable to the model.
    blocks = _sent_blocks(captured)
    assert blocks[-1] == {'type': 'text', 'text': ocr.INSTRUCTION}


def test_the_pages_go_up_as_labelled_images(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install(monkeypatch, _Message(stop_reason='end_turn', content=[_Block('text', '#')], model='m'))

    asyncio.run(anthropic_ocr.convert_pdf(_pdf(3)))

    blocks = _sent_blocks(captured)
    images = [block for block in blocks if block['type'] == 'image']
    labels = [block['text'] for block in blocks if block['type'] == 'text']
    assert len(images) == 3
    # The label is how a reply names a page; unlabelled, the pages are only an order.
    assert labels[:3] == ['Page 1:', 'Page 2:', 'Page 3:']
    assert all(base64.standard_b64decode(image['source']['data']).startswith(b'\x89PNG\r\n\x1a\n') for image in images)


def test_no_pdf_is_uploaded(monkeypatch: pytest.MonkeyPatch) -> None:
    # The text layer a `document` block carries is the channel this converter exists to avoid.
    captured = _install(monkeypatch, _Message(stop_reason='end_turn', content=[_Block('text', '#')], model='m'))

    asyncio.run(anthropic_ocr.convert_pdf(_pdf()))

    assert all(block['type'] != 'document' for block in _sent_blocks(captured))


# --- what a turn costs, and how it can fail ---


def test_the_turn_reports_what_it_cost(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # Nothing else records the tokens, so an unlogged turn cannot be priced after the fact.
    message = _Message(stop_reason='end_turn', content=[_Block('text', '#')], model='m', usage=_Usage())
    _install(monkeypatch, message)

    with caplog.at_level(logging.INFO, logger='themis.litcache.anthropic_ocr'):
        asyncio.run(anthropic_ocr.convert_pdf(_pdf()))

    (record,) = caplog.records
    assert message.usage.input_tokens in record.args  # type: ignore[operator]
    assert message.usage.output_tokens in record.args  # type: ignore[operator]


@pytest.mark.parametrize(('stop_reason', 'match'), [('max_tokens', 'truncated'), ('refusal', 'refused')])
def test_a_terminal_stop_raises_ocr_error(
    stop_reason: str, match: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Each yields an incomplete transcription that must not be committed, and re-asking only re-bills
    # it — and a turn that raises is still a turn that was paid for.
    message = _Message(stop_reason=stop_reason, content=[_Block('text', 'partial')], model='claude-sonnet-5')
    _install(monkeypatch, message)

    with (
        caplog.at_level(logging.INFO, logger='themis.litcache.anthropic_ocr'),
        pytest.raises(ocr.OcrError, match=match),
    ):
        asyncio.run(anthropic_ocr.convert_pdf(_pdf()))

    (record,) = caplog.records
    assert message.usage.input_tokens in record.args  # type: ignore[operator]
