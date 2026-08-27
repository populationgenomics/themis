"""LLM-OCR of a PDF source to markdown via an Anthropic vision model.

One of the interchangeable `ocr.PdfConverter` providers; the shared prompt, result and failure types
live in `ocr`. The producer (`themis.litcache.produce`) injects the converter, so it runs offline in
tests and the real call is exercised only in the deployed worker.

The transcription is minutes-long, so the request is streamed (the SDK refuses a non-streaming call it
estimates will outlast a ~10-minute idle-connection window) and the client timeout is raised to cover
a worst-case paper. The model id the API reports is read back off the response and recorded on the
rendering (`Rendering.model`), so the provenance is the model that actually produced the bytes, not
the one requested.
"""

from __future__ import annotations

import asyncio
import base64

import anthropic

from themis.litcache import ocr

# This provider's half of `Rendering.converter_version`: its model and request shape, not the shared
# prompt. Bump when either changes enough to alter the transcription.
HARNESS_VERSION = '1'

# Sonnet balances fidelity and cost for bounded transcription (dense multi-column papers, small
# captions, equations) against Opus; escalate the constant on observed fidelity gaps.
_MODEL = 'claude-sonnet-5'
# Under the Sonnet/Opus 128K output ceiling, with headroom for the newer tokenizer.
_MAX_TOKENS = 64000
# A minutes-long call must stream, and needs two bounds because httpx has no total-duration setting:
# it applies its timeout to connect/read/write/pool separately, and on a streamed call `read` is the
# gap between events. So `asyncio.timeout` bounds elapsed time and the client's bounds a silence.
#
# The two must differ, and by enough that a silence is caught first. `asyncio.timeout` is entered
# before the client connects, so an idle gap as long as the elapsed budget can never elapse inside it
# — given one value they would collapse to the elapsed bound alone, and a stream that died at minute
# five would settle the paper terminally instead of being retried.
#
# They classify differently, which is the point: the elapsed bound raises `OcrError` and settles the
# paper, because 28 minutes is the most the queue's dispatch deadline allows and a retry cannot have
# longer. A silence stays an `APITimeoutError` and propagates, because a dead connection says nothing
# about whether the paper is transcribable. `max_retries=0` keeps the client from tripling either.
# Both sit inside the convert worker's 30-minute request ceiling, leaving the GCS source read before
# and the manifest commit after room to finish.
_TIMEOUT_SECONDS = 1680.0
# The longest silence that is still a working stream. Generous against time-to-first-token on a large
# multi-image request, and far short of the elapsed bound so a dead connection is retried rather than
# settled. Raise it if a healthy transcription is ever seen to pause longer.
_STALL_SECONDS = 300.0


async def convert_pdf(pdf_bytes: bytes) -> ocr.OcrRendering:
    """Transcribe a research-paper PDF to markdown via an Anthropic vision model.

    Args:
        pdf_bytes: Raw PDF bytes. The API caps the request body at 32 MB and the document at 600
            pages (100 on 200K-context models); an over-limit PDF is a loud `BadRequestError`.

    Returns:
        The transcription and the model id that produced it.

    Raises:
        anthropic.BadRequestError: The PDF exceeds the API's page or size limits (HTTP 400/413).
        ocr.OcrError: The generation was truncated at the token ceiling, refused by the model, or ran
            past `_TIMEOUT_SECONDS` — each yields no transcription that can be committed as full text,
            and a retry would not change the outcome. The elapsed bound is terminal because it is the
            most the queue's dispatch deadline allows: a paper needing longer needs a different runner.
        anthropic.APITimeoutError: The connection could not be made, or the stream went silent for
            `_STALL_SECONDS`. Transient, so it propagates to be retried rather than settling the paper.
    """
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('ascii')
    # AsyncAnthropic() reads its credentials from the environment (in the deployed worker, the
    # workload-identity-federation ids — docs/runbooks/claude-api-wif.md); the client holds an httpx
    # connection pool, so it is closed with the request rather than left to the GC.
    try:
        async with (
            anthropic.AsyncAnthropic(timeout=_STALL_SECONDS, max_retries=0) as client,
            asyncio.timeout(_TIMEOUT_SECONDS),
            client.messages.stream(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'document',
                                'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': pdf_b64},
                            },
                            {'type': 'text', 'text': ocr.INSTRUCTION},
                        ],
                    }
                ],
            ) as stream,
        ):
            message = await stream.get_final_message()
    except TimeoutError as e:
        raise ocr.OcrError(f'PDF transcription exceeded {_TIMEOUT_SECONDS:.0f}s') from e

    if message.stop_reason == 'max_tokens':
        raise ocr.OcrError(f'PDF transcription truncated at max_tokens={_MAX_TOKENS}')
    if message.stop_reason == 'refusal':
        raise ocr.OcrError('model refused to transcribe the PDF')

    markdown = ''.join(block.text for block in message.content if block.type == 'text')
    return ocr.OcrRendering(
        markdown=markdown, model=message.model, converter_version=ocr.converter_version(HARNESS_VERSION)
    )
