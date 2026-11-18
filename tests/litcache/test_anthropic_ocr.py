"""Tests for the PDF LLM-OCR converter (`themis.litcache.anthropic_ocr`).

The Anthropic HTTP boundary is driven through `themis.testing.anthropic_sse` (`httpx2.MockTransport`),
exercising the real `anthropic.AsyncAnthropic` streaming parser, error mapping, `stop_reason` guards,
multi-block text join, and model read-back offline. Pages are rasterized for real, because the size a
page is rendered at is the size Claude reads it at, and a fake that skipped it would let a sizing bug
through.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import functools
import io
import pathlib
import subprocess
import sys
from collections.abc import Iterator

import anthropic
import httpx2
import pypdfium2
import pytest
from anthropic.lib import credentials as anthropic_credentials
from opentelemetry.sdk import metrics as sdk_metrics
from opentelemetry.sdk.metrics import export as metrics_export

from themis.litcache import anthropic_ocr, claude_images, ocr, pdf
from themis.telemetry import names, request_tokens
from themis.testing import anthropic_sse, in_memory_metrics

# A real paper, whose pages carry figures and dense two-column type: a blank page encodes to almost
# nothing, so only this says anything about what a request actually weighs.
_PAPER = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'oa' / 'source.pdf'

# The request body the API accepts, which the base64-encoded page images have to fit inside.
_MAX_REQUEST_BYTES = 32 * 1024 * 1024

_USAGE = anthropic_sse.Usage(input_tokens=4321, output_tokens=8765)


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


def _counter() -> tuple[request_tokens.RequestTokens, metrics_export.InMemoryMetricReader]:
    """A request-token counter and the reader its series are read back off."""
    reader = metrics_export.InMemoryMetricReader()
    provider = sdk_metrics.MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    return request_tokens.RequestTokens(provider.get_meter('test')), reader


def _input_series(*, model: str, stop_reason: str) -> in_memory_metrics.Labels:
    """The input-token series of a call whose prompt is a few thousand tokens, as every call here is."""
    return in_memory_metrics.labels(model=model, type='input', stop_reason=stop_reason, prompt_length='upTo100k')


def _tokens() -> request_tokens.RequestTokens:
    """A counter for a call whose tokens the test does not read back."""
    return _counter()[0]


def _convert(
    harness: anthropic_sse.Harness,
    pdf_bytes: bytes | None = None,
    *,
    tokens: ocr.TokenCounter | None = None,
    credentials: anthropic_ocr.CredentialsFactory | None = anthropic_sse.credentials,
    timeout: float | None = None,
) -> ocr.OcrRendering:
    async def run() -> ocr.OcrRendering:
        async with harness.http_client() as http_client:
            return await anthropic_ocr.convert_pdf(
                _pdf() if pdf_bytes is None else pdf_bytes,
                tokens=_tokens() if tokens is None else tokens,
                credentials=credentials,
                timeout=timeout,
                http_client=http_client,
            )

    return asyncio.run(run())


def test_a_call_the_model_answered_past_the_elapsed_bound_was_paid_for() -> None:
    # httpx2 has no total-duration timeout, so the client's own value would not have caught a call that keeps
    # streaming — only asyncio.timeout bounds elapsed time.
    harness = anthropic_sse.Harness([anthropic_sse.StalledStream(after_start=True, model='m', usage=_USAGE)])
    tokens, reader = _counter()

    with pytest.raises(ocr.SpentError, match='exceeded'):
        _convert(harness, _pdf(1), tokens=tokens, timeout=0.5)

    counted = in_memory_metrics.collected(reader)[names.REQUEST_TOKENS]
    assert counted[_input_series(model='m', stop_reason='unfinished')] == 4321


def test_a_call_past_the_elapsed_bound_before_the_model_answered_was_not() -> None:
    harness = anthropic_sse.Harness([anthropic_sse.StalledStream(after_start=False)])
    tokens, reader = _counter()

    with pytest.raises(TimeoutError):
        _convert(harness, _pdf(1), tokens=tokens, timeout=0.1)

    assert names.REQUEST_TOKENS not in in_memory_metrics.collected(reader)


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
    (page,) = asyncio.run(anthropic_ocr.render(_pdf(1)))
    image = (page.width, page.height)

    assert (
        claude_images.resized_size(
            *image,
            max_edge=claude_images.HIGH_RESOLUTION.max_edge,
            max_tokens=claude_images.HIGH_RESOLUTION.max_tokens,
        )
        == image
    )


def test_a_paper_whose_pages_together_overrun_is_rendered_small_enough_to_send() -> None:
    # What one page weighs is not what twenty weigh, and the API rejects the whole request rather
    # than trimming it. The budget is forced because the fixture no longer overruns on its own: at
    # twenty pages it encodes to ~25 MB of the 32 MB a request carries, and past twenty the
    # many-image edge cap shrinks the pages before their weight ever does.
    (alone,) = asyncio.run(anthropic_ocr.render(_paper_of(1)))
    twenty = asyncio.run(anthropic_ocr.render(_paper_of(20)))
    assert sum(len(page.png) for page in twenty) * 4 / 3 < _MAX_REQUEST_BYTES

    with _budget(sum(len(page.png) for page in twenty) - 1):
        pages = asyncio.run(anthropic_ocr.render(_paper_of(20)))

    assert len(pages) == 20
    assert pages[0].width < alone.width


def test_a_page_too_heavy_to_send_is_rendered_smaller_before_it_is_given_up_on() -> None:
    # A budget just under one page's own weight drives the loop's body, not only its exit. A real
    # page, not a blank one: blank pages encode to almost nothing at any size, so shrinking one
    # frees no bytes and the loop has nothing to show.
    (full,) = asyncio.run(anthropic_ocr.render(_paper_of(1)))

    with _budget(len(full.png) - 1):
        (shrunk,) = asyncio.run(anthropic_ocr.render(_paper_of(1)))

    assert shrunk.width < full.width


def test_an_unloadable_pdf_is_terminal_not_retried() -> None:
    # The same bytes will not load next time. Left as a PdfiumError it is a 5xx, five Cloud Tasks
    # retries, and a paper stuck PENDING with nothing recording why: produce.py settles OcrError alone.
    with pytest.raises(ocr.OcrError, match='could not be loaded'):
        asyncio.run(anthropic_ocr.render(b'not a pdf'))


def _raise(error: Exception) -> object:
    """A stand-in for a `pdf` job submitter whose job fails the way the real one would."""

    def fail(*_args: object, **_kwargs: object) -> concurrent.futures.Future[object]:
        failed: concurrent.futures.Future[object] = concurrent.futures.Future()
        failed.set_exception(error)
        return failed

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
        asyncio.run(anthropic_ocr.render(_paper_of(1)))


def test_the_shrink_makes_progress_when_the_token_budget_binds() -> None:
    # _PageFit solves against the tier's token budget as well as its edge. On US letter the budget
    # binds, so scaling the previous cap fits to the same pixels and the loop reads "unchanged" as
    # "nothing left to try" — settling a paper that a smaller render would have sent.
    letter = _pdf(1)  # 612x792, the ratio at which the token budget binds before the edge does
    (full,) = asyncio.run(anthropic_ocr.render(letter))

    with _budget(len(full.png) - 1):
        (shrunk,) = asyncio.run(anthropic_ocr.render(letter))

    assert shrunk.height < full.height


def test_a_paper_that_will_not_fit_a_request_is_terminal() -> None:
    # Shrinking stops at the standard tier's edge rather than rendering something illegible, and a
    # paper still too large there will not transcribe however many times it is asked.
    with _budget(1), pytest.raises(ocr.OcrError, match='one request'):
        asyncio.run(anthropic_ocr.render(_pdf(1)))


def test_a_paper_of_more_pages_than_a_request_carries_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Image blocks are capped per request, and no amount of shrinking removes a page.
    monkeypatch.setattr(claude_images, 'MAX_IMAGE_BLOCKS', 2)

    with pytest.raises(ocr.OcrError, match='image blocks'):
        asyncio.run(anthropic_ocr.render(_pdf(3)))


@pytest.mark.parametrize(
    ('pages', 'capped'),
    [(claude_images.MANY_IMAGE_THRESHOLD, False), (claude_images.MANY_IMAGE_THRESHOLD + 1, True)],
)
def test_the_many_image_cap_turns_on_above_the_threshold_and_not_at_it(pages: int, capped: bool) -> None:
    # "More than twenty" is an off-by-one waiting to happen, and getting it wrong either wastes
    # resolution on every long paper or has the API reject it outright.
    rendered = asyncio.run(anthropic_ocr.render(_pdf(pages)))

    assert all(max(page.width, page.height) <= claude_images.MANY_IMAGE_MAX_EDGE for page in rendered) == capped


# --- the request ---


def test_the_supplied_credential_is_the_one_the_client_authenticates_with(monkeypatch: pytest.MonkeyPatch) -> None:
    # The credential has to reach the client, and be built per call: an explicit one is total (the
    # client then reads no credential env var), and the client closes the provider it is handed.
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'ignored-env-key')
    built: list[int] = []

    def credentials() -> anthropic_credentials.AccessTokenProvider:
        index = len(built) + 1
        built.append(index)
        return lambda *, force_refresh=False: anthropic_credentials.AccessToken(  # noqa: ARG005
            token=f'supplied-token-{index}'
        )

    ok = anthropic_sse.MessageTurn(content=[anthropic_sse.Text('#')], model='m')
    harness = anthropic_sse.Harness([ok, ok])

    _convert(harness, _pdf(1), credentials=credentials)
    _convert(harness, _pdf(1), credentials=credentials)

    assert len(built) == 2
    assert [r.headers.get('authorization') for r in harness.requests] == [
        'Bearer supplied-token-1',
        'Bearer supplied-token-2',
    ]
    assert all('x-api-key' not in r.headers for r in harness.requests)


def test_no_factory_leaves_the_client_without_an_explicit_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit credential is what makes the SDK consult the environment; anything else would pin
    # the call to a provider the caller never chose.
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'env-api-key')
    harness = anthropic_sse.Harness([anthropic_sse.MessageTurn(content=[anthropic_sse.Text('#')], model='m')])

    _convert(harness, _pdf(1), credentials=None)

    assert harness.requests[0].headers['x-api-key'] == 'env-api-key'


def test_convert_pdf_joins_text_blocks_and_reads_back_the_response_model() -> None:
    # A non-text block is skipped; the response's own model id is recorded, not the requested one.
    turn = anthropic_sse.MessageTurn(
        stop_reason='end_turn',
        content=[anthropic_sse.Text('# Title\n'), anthropic_sse.Thinking('ignored'), anthropic_sse.Text('Body.')],
        model='claude-sonnet-5-20260101',
    )
    harness = anthropic_sse.Harness([turn])

    result = _convert(harness)

    assert result.markdown == '# Title\nBody.'
    assert result.model == 'claude-sonnet-5-20260101'
    # The shared prompt version leads, this provider's own request shape follows — a prompt change
    # supersedes every provider's renderings, a request-shape change only this one's.
    assert result.converter_version == '1.1'
    # Against the shared instruction, not one of its own: holding the prompt fixed across providers is
    # what makes a transcription difference attributable to the model.
    assert harness.requests[0].content_blocks()[-1] == {'type': 'text', 'text': ocr.INSTRUCTION}


def test_the_pages_go_up_as_labelled_images() -> None:
    harness = anthropic_sse.Harness([anthropic_sse.MessageTurn(content=[anthropic_sse.Text('#')], model='m')])

    _convert(harness, _pdf(3))

    blocks = harness.requests[0].content_blocks()
    images = [block for block in blocks if block['type'] == 'image']
    labels = [block['text'] for block in blocks if block['type'] == 'text']
    assert len(images) == 3
    # The label is how a reply names a page; unlabelled, the pages are only an order.
    assert labels[:3] == ['Page 1:', 'Page 2:', 'Page 3:']
    assert all(
        isinstance(image['source'], dict)
        and isinstance(image['source']['data'], str)
        and base64.standard_b64decode(image['source']['data']).startswith(b'\x89PNG\r\n\x1a\n')
        for image in images
    )


def test_no_pdf_is_uploaded() -> None:
    # The text layer a `document` block carries is the channel this converter exists to avoid.
    harness = anthropic_sse.Harness([anthropic_sse.MessageTurn(content=[anthropic_sse.Text('#')], model='m')])

    _convert(harness)

    assert all(block['type'] != 'document' for block in harness.requests[0].content_blocks())


# --- what a turn costs, and how it can fail ---


def test_the_turn_reports_what_it_cost() -> None:
    # The counter is where the spend is priced from.
    usage = anthropic_sse.Usage(
        input_tokens=4321,
        output_tokens=8765,
        cache_read_input_tokens=7,
        cache_creation_input_tokens=9,
    )
    turn = anthropic_sse.MessageTurn(
        stop_reason='end_turn',
        content=[anthropic_sse.Text('#')],
        model='claude-sonnet-5-20260101',
        usage=usage,
    )
    harness = anthropic_sse.Harness([turn])
    tokens, reader = _counter()

    _convert(harness, tokens=tokens)

    series = functools.partial(
        in_memory_metrics.labels, model='claude-sonnet-5-20260101', stop_reason='end_turn', prompt_length='upTo100k'
    )
    assert in_memory_metrics.collected(reader)[names.REQUEST_TOKENS] == {
        series(type='input'): 4321,
        series(type='output'): 8765,
        series(type='cacheRead'): 7,
        series(type='cacheCreation'): 9,
    }


@pytest.mark.parametrize(('stop_reason', 'match'), [('max_tokens', 'truncated'), ('refusal', 'refused')])
def test_a_terminal_stop_raises_ocr_error(stop_reason: str, match: str) -> None:
    # Each yields an incomplete transcription that must not be committed, and re-asking only re-bills
    # it — and a turn that raises is still a turn that was paid for.
    turn = anthropic_sse.MessageTurn(
        stop_reason=stop_reason,
        content=[anthropic_sse.Text('partial')],
        model='claude-sonnet-5',
        usage=_USAGE,
    )
    harness = anthropic_sse.Harness([turn])
    tokens, reader = _counter()

    with pytest.raises(ocr.OcrError, match=match):
        _convert(harness, tokens=tokens)

    counted = in_memory_metrics.collected(reader)[names.REQUEST_TOKENS]
    assert counted[_input_series(model='claude-sonnet-5', stop_reason=stop_reason)] == 4321


def test_a_final_message_without_a_stop_reason_is_a_precondition_failure() -> None:
    # The API sets one on every complete turn; without it neither the counter's label nor the terminal
    # checks have anything to read, so the turn is refused rather than committed as a success.
    harness = anthropic_sse.Harness(
        [anthropic_sse.MessageTurn(stop_reason=None, content=[anthropic_sse.Text('#')], model='m')]
    )

    with pytest.raises(ocr.SpentError, match='stop_reason'):
        _convert(harness)


_WORKSPACE_LIMIT = anthropic_sse.StatusError(
    status_code=400,
    error_type='invalid_request_error',
    message='You have reached your specified workspace API usage limits. You will regain access on 2026-11-01.',
)
_ORGANIZATION_LIMIT = anthropic_sse.StatusError(
    status_code=400,
    error_type='invalid_request_error',
    message='You have reached your specified API usage limits.',
)
_SPEND_CAP = anthropic_sse.StatusError(
    status_code=429,
    error_type='rate_limit_error',
    message='You have reached your API usage limits.',
    details={'error_code': 'enforced_spend_limit_reached'},
)
_BILLING = anthropic_sse.StatusError(
    status_code=402,
    error_type='billing_error',
    message='Your credit balance is too low.',
)
_RATE_LIMIT = anthropic_sse.StatusError(
    status_code=429,
    error_type='rate_limit_error',
    message='Number of request tokens has exceeded.',
)
_TOO_MANY_IMAGES = anthropic_sse.StatusError(
    status_code=400,
    error_type='invalid_request_error',
    message='too many images',
)


@pytest.mark.parametrize(
    ('turn', 'cause_cls'),
    [
        pytest.param(_WORKSPACE_LIMIT, anthropic.BadRequestError, id='workspace limit'),
        pytest.param(_ORGANIZATION_LIMIT, anthropic.BadRequestError, id='organization limit'),
        pytest.param(_SPEND_CAP, anthropic.RateLimitError, id='monthly spend cap'),
        pytest.param(_BILLING, anthropic.APIStatusError, id='billing'),
    ],
)
def test_a_call_refused_at_a_spend_limit_is_refused_not_failed(
    turn: anthropic_sse.StatusError, cause_cls: type[anthropic.APIStatusError]
) -> None:
    harness = anthropic_sse.Harness([turn])

    with pytest.raises(ocr.SpendRefusedError) as raised:
        _convert(harness)
    assert isinstance(raised.value.__cause__, cause_cls)


@pytest.mark.parametrize(
    ('turn', 'expected_cls'),
    [
        pytest.param(_RATE_LIMIT, anthropic.RateLimitError, id='a rate limit'),
        pytest.param(_TOO_MANY_IMAGES, anthropic.BadRequestError, id='a request the API refuses'),
        pytest.param(anthropic_sse.ConnectFailure(), anthropic.APIConnectionError, id='no connection'),
    ],
)
def test_a_call_failing_before_the_model_answered_propagates_as_itself(
    turn: anthropic_sse.Turn, expected_cls: type[anthropic.APIError]
) -> None:
    harness = anthropic_sse.Harness([turn])

    with pytest.raises(expected_cls):
        _convert(harness)


def test_a_stream_failing_before_message_start_was_not_paid_for() -> None:
    turn = anthropic_sse.StreamError(error_type='overloaded_error', message='Overloaded', after_start=False)
    harness = anthropic_sse.Harness([turn])
    tokens, reader = _counter()

    with pytest.raises(anthropic.APIStatusError) as raised:
        _convert(harness, tokens=tokens)
    assert raised.value.type == 'overloaded_error'
    assert names.REQUEST_TOKENS not in in_memory_metrics.collected(reader)


@pytest.mark.parametrize(
    ('turn', 'cause_cls'),
    [
        pytest.param(
            anthropic_sse.StreamError(error_type='overloaded_error', message='Overloaded', model='m', usage=_USAGE),
            anthropic.APIStatusError,
            id='an error event',
        ),
        pytest.param(
            anthropic_sse.StreamError(
                error_type='billing_error', message='Your credit balance is too low.', model='m', usage=_USAGE
            ),
            anthropic.APIStatusError,
            id='a billing error event',
        ),
        pytest.param(
            anthropic_sse.DroppedStream(
                error=httpx2.RemoteProtocolError('peer closed connection'), model='m', usage=_USAGE
            ),
            httpx2.RemoteProtocolError,
            id='a dropped connection',
        ),
        pytest.param(
            anthropic_sse.DroppedStream(error=httpx2.ReadTimeout('timed out'), model='m', usage=_USAGE),
            httpx2.ReadTimeout,
            id='a silence',
        ),
    ],
)
def test_a_stream_failing_after_message_start_was_paid_for(
    turn: anthropic_sse.Turn, cause_cls: type[Exception]
) -> None:
    harness = anthropic_sse.Harness([turn])
    tokens, reader = _counter()

    with pytest.raises(ocr.SpentError) as raised:
        _convert(harness, tokens=tokens)
    assert isinstance(raised.value.__cause__, cause_cls)
    # Paid for, so counted, as far as the stream got.
    counted = in_memory_metrics.collected(reader)[names.REQUEST_TOKENS]
    assert counted[_input_series(model='m', stop_reason='unfinished')] == 4321


def test_a_stream_blocked_after_message_start_settles_and_was_paid_for() -> None:
    blocked = anthropic_sse.StreamError(
        error_type='invalid_request_error',
        message='Output blocked by content filtering policy',
        model='m',
        usage=_USAGE,
    )
    harness = anthropic_sse.Harness([blocked])
    tokens, reader = _counter()

    # Terminal, not spent: the same pages are blocked again, so a retry only re-bills them.
    with pytest.raises(ocr.OcrError, match='rejected mid-stream') as raised:
        _convert(harness, tokens=tokens)
    assert isinstance(raised.value.__cause__, anthropic.APIStatusError)
    counted = in_memory_metrics.collected(reader)[names.REQUEST_TOKENS]
    assert counted[_input_series(model='m', stop_reason='unfinished')] == 4321


def test_an_sse_error_event_after_message_start_is_read_as_a_block() -> None:
    # The request-id header from the blocked SSE turn surfaces in the permanent OcrError message.
    blocked = anthropic_sse.StreamError(
        error_type='invalid_request_error',
        message='Output blocked by content filtering policy',
        model='m',
        usage=_USAGE,
        request_id='req_blocked',
    )
    harness = anthropic_sse.Harness([blocked])

    with pytest.raises(ocr.OcrError, match='req_blocked'):
        _convert(harness)


# --- what the converter depends on ---


def test_the_converter_does_not_import_the_metrics_pipeline() -> None:
    # litcache ships in images that carry neither `themis/telemetry` nor the OpenTelemetry packages; the
    # counter reaches it as a `TokenCounter` the worker binds, never as an import.
    probe = (
        'import sys, themis.litcache.anthropic_ocr; '
        "print(sorted(name for name in sys.modules if name.startswith('themis.telemetry')))"
    )
    completed = subprocess.run(  # noqa: S603 — the interpreter running this suite, a fixed argv
        [sys.executable, '-c', probe], check=True, capture_output=True, text=True
    )
    assert completed.stdout.strip() == '[]'
