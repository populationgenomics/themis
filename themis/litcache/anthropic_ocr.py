"""LLM-OCR of a PDF source to markdown via an Anthropic vision model.

One of the interchangeable `ocr.PdfConverter` providers; the shared prompt, result and failure types
live in `ocr`. The producer (`themis.litcache.produce`) injects the converter, so it runs offline in
tests and the real call is exercised only in the deployed worker.

The pages go up as rendered images, never as a pdf. A pdf sent whole puts its embedded text layer in
front of the model, and that layer is sometimes wrong — where it is, the transcription inherits the
error instead of reading what is printed. Rasterizing here transcribes the page as it appears, sized
by `claude_images` so Claude does not resize it again on arrival.

The transcription is minutes-long, so the request is streamed (the SDK refuses a non-streaming call it
estimates will outlast a ~10-minute idle-connection window) and the client timeout is raised to cover
a worst-case paper. The model id the API reports is read back off the response and recorded on the
rendering (`Rendering.model`), so the provenance is the model that actually produced the bytes, not
the one requested.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable, Sequence

import anthropic
import anthropic.types
import pypdfium2
from anthropic.lib import credentials as anthropic_credentials

from themis.litcache import claude_images, ocr, pdf

_LOG = logging.getLogger(__name__)

CredentialsFactory = Callable[[], anthropic_credentials.AccessTokenProvider]
"""Builds the access-token provider one call authenticates with.

A factory rather than a provider because the client closes the provider it was handed, so a single
instance would be spent after one transcription. The caller that supplies it decides how the call
authenticates — the convert worker binds workload identity federation
(`themis/services/convert_worker/__main__.py`)."""

# This provider's half of `Rendering.converter_version`: its model and request shape, not the shared
# prompt. Bump when either changes enough that renderings already recorded should be re-made.
HARNESS_VERSION = '1'

# Sonnet balances fidelity and cost for bounded transcription (dense multi-column papers, small
# captions, equations) against Opus; escalate the constant on observed fidelity gaps.
_MODEL = 'claude-sonnet-5'
# Under the Sonnet/Opus 128K output ceiling, with headroom for the newer tokenizer.
_MAX_TOKENS = 64000
# A minutes-long call must stream, and needs two bounds because httpx2 has no total-duration setting:
# it applies its timeout to connect/read/write/pool separately, and on a streamed call `read` is the
# gap between events. So `asyncio.timeout` bounds elapsed time and the client's bounds a silence.
#
# The two must differ, and by enough that a silence is caught first. `asyncio.timeout` is entered
# before the client connects, so an idle gap as long as the elapsed budget can never elapse inside it
# — given one value they would collapse to the elapsed bound alone, and a stream that died at minute
# five would settle the paper terminally instead of being retried.
#
# They classify differently, which is the point: the elapsed bound is reached only by a stream the
# model answered, so it raises `SpentError` and counts towards the paper's bound on paid attempts. It is
# not terminal: 28 minutes is the longest any attempt can have, but how long a turn takes varies, so a
# paper past it once may finish on another attempt, and the job log's bound of three caps the cost. A
# silence propagates to be retried, because a dead connection says nothing about whether the paper is
# transcribable: before the response arrives the SDK raises it as `APITimeoutError`, uncounted, and
# once the stream is being read it surfaces as httpx2's own `ReadTimeout`, which after `message_start`
# is paid for. `max_retries=0` keeps the client from tripling either.
# Both sit inside the convert worker's 30-minute request ceiling, leaving the GCS source read before
# and the manifest commit after room to finish.
_TIMEOUT_SECONDS = 1680.0
# The longest silence that is still a working stream. Generous against time-to-first-token on a large
# multi-image request, and far short of the elapsed bound so a dead connection is retried rather than
# settled. Raise it if a healthy transcription is ever seen to pause longer.
_STALL_SECONDS = 300.0

# Claude 4.7 and later read at the high-resolution tier; pages are rendered as large as it allows.
_TIER = claude_images.HIGH_RESOLUTION

# The density a page is measured at before the tier search shrinks it. High enough that the search,
# not this, decides the size — a page seeded below the tier ceiling would be rendered small.
_SEED_DPI = 400
_POINTS_PER_INCH = 72

# Both limits are on the base64 the request carries, so they become smaller budgets for the png bytes
# held here. The request body caps at 32 MB, less a margin for the prompt, the page labels and the
# JSON envelope around the images.
_BASE64_RATIO = 4 / 3
_PAGE_BYTE_BUDGET = int(32 * 1024 * 1024 * 0.95 / _BASE64_RATIO)
_IMAGE_BYTE_BUDGET = int(claude_images.MAX_IMAGE_BASE64_BYTES / _BASE64_RATIO)
# Each attempt rasterizes the whole document again, so the step is coarse rather than a search.
_SHRINK = 0.85

# How the API says a spend limit refused the call (https://platform.claude.com/docs/en/api/rate-limits):
# a limit set on the workspace or the organization answers 400 with one of these messages; the
# organization's monthly cap answers 429 with this error code, which a rate limit does not carry.
_SPEND_LIMIT_MESSAGES = (
    'You have reached your specified workspace API usage limits',
    'You have reached your specified API usage limits',
)
_SPEND_CAP_ERROR_CODE = 'enforced_spend_limit_reached'
# The stop-reason label a turn's tokens are counted under when it failed before the model finished it.
_UNFINISHED = 'unfinished'
# An exhausted credit balance, or other billing fault.
_BILLING_ERROR_TYPE = 'billing_error'
# An error event of this type after `message_start` judges the output, not the service. The content
# filter's "Output blocked by content filtering policy" is one, and has been seen to block the same pages
# on every attempt.
_OUTPUT_REJECTED_ERROR_TYPE = 'invalid_request_error'


def _max_edge(page_count: int) -> int:
    """The longest edge a page may have, for a request carrying `page_count` images."""
    # Past twenty images every image in the request must also stay under a smaller edge, or the
    # request is rejected outright rather than downscaled.
    if page_count > claude_images.MANY_IMAGE_THRESHOLD:
        return min(_TIER.max_edge, claude_images.MANY_IMAGE_MAX_EDGE)
    return _TIER.max_edge


def _page_fit(max_edge: int) -> pdf.PageFit:
    """The size to render each page at, within `max_edge` and the tier's visual-token budget."""

    def fit(width_pt: float, height_pt: float) -> tuple[int, int]:
        scale = _SEED_DPI / _POINTS_PER_INCH
        # Fit to what Claude would resize the image to, so it reads the page as rendered rather than
        # a resample of it — and so a returned coordinate is in the space we chose.
        _, page_height = claude_images.resized_size(
            max(round(width_pt * scale), 1),
            max(round(height_pt * scale), 1),
            max_edge=max_edge,
            max_tokens=_TIER.max_tokens,
        )
        return (max(round(page_height * width_pt / height_pt), 1), page_height)

    return fit


def render(pdf_bytes: bytes) -> list[pdf.PageImage]:
    """Rasterize a paper's pages at the largest size this provider can both read and send.

    How many bytes a page encodes to depends on what is printed on it, not only on its size, so the
    size limits are measured rather than predicted: a paper whose pages overrun what one request
    carries is rendered again smaller until they fit. Most papers are rendered once.

    Raises:
        ocr.OcrError: The pdf does not load, has no pages, carries a page that cannot be
            rasterized, or the paper does not fit one request
            — too many pages to send as image blocks, or pages still too large at the standard tier's
            edge. Rendering it again would produce the same bytes, so this is settled, not retried.
    """
    try:
        page_count = pdf.page_count(pdf_bytes)
    except pypdfium2.PdfiumError as e:
        # Permanent: the same bytes will not load next time. Left to propagate it is a 5xx, five
        # retries, and a paper stuck PENDING with nothing recording why.
        raise ocr.OcrError(f'the pdf source could not be loaded: {e}') from e
    if not page_count:
        raise ocr.OcrError('the pdf source has no pages')
    if page_count > claude_images.MAX_IMAGE_BLOCKS:
        raise ocr.OcrError(
            f'{page_count} pages exceeds the {claude_images.MAX_IMAGE_BLOCKS} image blocks one request carries'
        )
    floor = claude_images.STANDARD_RESOLUTION.max_edge
    max_edge = _max_edge(page_count)
    rendered_at: tuple[int, int] | None = None
    while True:
        try:
            pages = pdf.render_pages(pdf_bytes, fit=_page_fit(max_edge))
        except (ValueError, pypdfium2.PdfiumError) as e:
            # A page the document's page tree offers but pdfium will not load or rasterize:
            # `FPDF_LoadPage` returning NULL is a `PdfiumError`, a degenerate page size a
            # `ValueError`. Both are permanent, so they settle here rather than propagating as a 5xx
            # into retries that render the same page again.
            raise ocr.OcrError(f'the pdf source has an unrenderable page: {e}') from e
        encoded = sum(len(page.png) for page in pages)
        largest = max(len(page.png) for page in pages)
        if encoded <= _PAGE_BYTE_BUDGET and largest <= _IMAGE_BYTE_BUDGET:
            return pages
        size = (pages[0].width, pages[0].height)
        # The next edge comes off the rendered page, not off the previous cap. `_page_fit` solves
        # against the tier's token budget as well as the edge, and when the budget is the binding one
        # a smaller cap fits to the same pixels and renders the same bytes -- on US letter it does,
        # so scaling the cap alone stalls on the commonest page there is.
        if max_edge <= floor or size == rendered_at:
            raise ocr.OcrError(
                f'{page_count} pages encode to {encoded} bytes at {size[0]}x{size[1]} (largest page '
                f'{largest}), over what one request carries'
            )
        rendered_at = size
        max_edge = max(int(max(size) * _SHRINK), floor)
        _LOG.info('%d pages encode to %d bytes; re-rendering at a %dpx edge', page_count, encoded, max_edge)


def _page_blocks(
    pages: Sequence[pdf.PageImage],
) -> list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam]:
    """The page images as content blocks, each labelled with the page it is."""
    blocks: list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam] = []
    for page in pages:
        blocks.append({'type': 'text', 'text': f'Page {page.number}:'})
        blocks.append(
            {
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/png',
                    'data': base64.standard_b64encode(page.png).decode('ascii'),
                },
            }
        )
    return blocks


def _spend_refused(error: anthropic.APIStatusError) -> bool:
    """Whether the API refused the call at a spend limit or for billing, rather than for the request or a rate."""
    inner = error.body.get('error') if isinstance(error.body, dict) else None
    if not isinstance(inner, dict):
        return False
    message = inner.get('message')
    details = inner.get('details')
    if isinstance(error, anthropic.BadRequestError):
        return isinstance(message, str) and message.startswith(_SPEND_LIMIT_MESSAGES)
    if isinstance(error, anthropic.RateLimitError):
        return isinstance(details, dict) and details.get('error_code') == _SPEND_CAP_ERROR_CODE
    return error.type == _BILLING_ERROR_TYPE


async def convert_pdf(
    pdf_bytes: bytes, *, tokens: ocr.TokenCounter, credentials: CredentialsFactory | None = None
) -> ocr.OcrRendering:
    """Transcribe a research-paper PDF to markdown via an Anthropic vision model.

    Args:
        pdf_bytes: Raw PDF bytes. Rasterized here, so what bounds a paper is the image-block and
            size limits on one request rather than the document limits (`render`).
        tokens: The counter the turn's token usage is added to, whatever its stop reason: a direct
            call's spend is observable nowhere but here. A turn that fails after the model answered is
            counted as far as the stream got, under the stop reason `unfinished`.
        credentials: Builds the access-token provider for this call. `None` leaves the SDK to
            resolve credentials from the environment its own way.

    Returns:
        The transcription and the model id that produced it.

    Raises:
        anthropic.BadRequestError: The request was rejected on a limit `render` does not check —
            a shorter block ceiling on a smaller-context model, say (HTTP 400/413).
        ocr.OcrError: The generation was truncated at the token ceiling, refused by the model, or blocked
            mid-stream by an `invalid_request_error` event, or the pdf will not render into one request
            (`render`) — none yields a transcription that can be committed as full text, and a retry
            would not change the outcome. A blocked turn's tokens are counted under `unfinished`.
        ocr.SpendRefusedError: The API refused the call at a spend limit, or for billing; nothing was spent.
        ocr.SpentError: The call failed in any way after the stream's `message_start`, the sign the model
            answered and the call was paid for: it ran past `_TIMEOUT_SECONDS`, the stream broke or went
            silent, or an error event other than a block arrived. The original error is its cause.
        anthropic.APIError: The call failed before the model answered — the connection could not be made,
            the API refused the request, or it was silent for `_STALL_SECONDS` before responding.
            Transient or not, it cost nothing, so it propagates to be retried, as does any other error
            raised before `message_start`.
        anthropic.lib.credentials.WorkloadIdentityError: `credentials` could not be exchanged for an
            access token. Not an `OcrError`, so it propagates and the paper is retried rather than
            written off over our own credential.
        ocr.SpentError: Also when the final message carries no stop reason, which the API sets on every
            complete turn: the turn was paid for and cannot be judged.
    """
    pages = render(pdf_bytes)
    answered = False
    # The turn as far as the stream got, from `message_start` on: what a turn that fails after it was paid for.
    snapshot: anthropic.types.Message | None = None
    # An explicit provider is total: given one, the client reads no credential env var at all. Given
    # none, it resolves from the environment. The client closes the provider it was given, along with
    # its own connection pool.
    try:
        async with (
            anthropic.AsyncAnthropic(
                credentials=credentials() if credentials is not None else None,
                timeout=_STALL_SECONDS,
                max_retries=0,
            ) as client,
            asyncio.timeout(_TIMEOUT_SECONDS),
            client.messages.stream(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[
                    {'role': 'user', 'content': [*_page_blocks(pages), {'type': 'text', 'text': ocr.INSTRUCTION}]}
                ],
            ) as stream,
        ):
            async for event in stream:
                answered = answered or event.type == 'message_start'
                if answered:
                    snapshot = stream.current_message_snapshot
            message = await stream.get_final_message()
    except TimeoutError as e:
        if snapshot is None:
            raise
        tokens.add(snapshot.usage, model=snapshot.model, stop_reason=_UNFINISHED)
        raise ocr.SpentError(f'PDF transcription exceeded {_TIMEOUT_SECONDS:.0f}s') from e
    except Exception as e:
        # Once the stream is being read the SDK wraps nothing: a silence or a dropped connection arrives as
        # httpx2's own error, so whatever fails after `message_start` is a paid call that failed.
        if snapshot is not None:
            tokens.add(snapshot.usage, model=snapshot.model, stop_reason=_UNFINISHED)
            if isinstance(e, anthropic.APIStatusError) and e.type == _OUTPUT_REJECTED_ERROR_TYPE:
                raise ocr.OcrError(f'the transcription was rejected mid-stream (request {e.request_id}): {e}') from e
            raise ocr.SpentError(f'the transcription stream failed after the model answered: {e!r}') from e
        if isinstance(e, anthropic.APIStatusError) and _spend_refused(e):
            raise ocr.SpendRefusedError(str(e)) from e
        raise

    usage = message.usage
    if message.stop_reason is None:
        tokens.add(usage, model=message.model, stop_reason=_UNFINISHED)
        raise ocr.SpentError('the final message carries no stop_reason')
    # Recorded before the stop reason is judged: a truncated turn is billed like any other.
    tokens.add(usage, model=message.model, stop_reason=message.stop_reason)
    _LOG.info(
        'transcription turn on %s over %d pages: %s input (%s cache read, %s cache write), %s output tokens',
        message.model,
        len(pages),
        usage.input_tokens,
        usage.cache_read_input_tokens or 0,
        usage.cache_creation_input_tokens or 0,
        usage.output_tokens,
    )

    if message.stop_reason == 'max_tokens':
        raise ocr.OcrError(f'PDF transcription truncated at max_tokens={_MAX_TOKENS}')
    if message.stop_reason == 'refusal':
        raise ocr.OcrError('model refused to transcribe the PDF')

    markdown = ''.join(block.text for block in message.content if block.type == 'text')
    return ocr.OcrRendering(
        markdown=markdown, model=message.model, converter_version=ocr.converter_version(HARNESS_VERSION)
    )
