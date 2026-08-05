"""LLM-OCR of a PDF source to markdown — the slow pdf-without-xml conversion branch.

When the OA ladder serves no usable XML, a paper's full text can still be recovered by transcribing
its PDF with an Anthropic vision model (`docs/design/evidence-fulltext.md`). This isolates that
Anthropic call behind one async function so the producer (`themis.litcache.produce`) injects it and
runs offline in tests; the real call touches the network and is exercised only in the deployed worker.

The transcription is minutes-long, so the request is streamed (the SDK refuses a non-streaming call it
estimates will outlast a ~10-minute idle-connection window) and the client timeout is raised to cover
a worst-case paper. The model id the API reports is read back off the response and recorded on the
rendering (`Rendering.model`), so the provenance is the model that actually produced the bytes, not
the one requested.
"""

from __future__ import annotations

import base64
import dataclasses

import anthropic

# The converter harness version recorded as `Rendering.converter_version`: this module's prompt and
# invocation, not a package version (there is no package — the "converter" is Anthropic + this code).
# Bump when the prompt or request shape changes enough to alter the transcription.
HARNESS_VERSION = '1'

# Sonnet balances fidelity and cost for bounded transcription (dense multi-column papers, small
# captions, equations) against Opus; escalate the constant on observed fidelity gaps.
_MODEL = 'claude-sonnet-5'
# Under the Sonnet/Opus 128K output ceiling, with headroom for the newer tokenizer.
_MAX_TOKENS = 64000
# A minutes-long call must stream; the timeout bounds the whole call, `max_retries=0` keeps it from
# silently tripling on a transient timeout.
_TIMEOUT_SECONDS = 1800.0

_INSTRUCTION = r"""Carefully transcribe this research-paper PDF to GitHub-flavored Markdown. The output is
the canonical rendering downstream tools quote against, so it must faithfully mimic the layout and
hierarchy of the original while reading as clean body text.

- Do not include headers or footers repeated on each page, page numbers, or the PDF's own margin line numbers.
- Preserve the reading order as it appears (columns top-to-bottom, left-to-right; never interleave).
- Remove hyphens that break a word across a line end ("uti- lized" -> "utilized").
- Use Markdown headings (#, ##, ###) reflecting the title and section hierarchy.
- Put a blank line before and after every heading, list, and table; end each paragraph with a blank line;
  do not break lines within a paragraph or heading.
- Render bullet and numbered/lettered lists as Markdown lists; use blockquotes for sidebars or pull quotes.
- Preserve the bold and italic emphasis of the original.
- Render tables as GitHub-flavored Markdown, copying cell values and identifiers exactly; convert a table
  embedded in an image into Markdown too.
- Transcribe figure and table captions verbatim; do not describe image contents or add commentary.
- Render all mathematical equations and symbols as LaTeX enclosed in `$` (inline) or `$$` (display) — write
  `\alpha`, not the Greek letter, and `\cos`, not `cos`; take care to distinguish Latin from Greek letters.

Output only the transcription, with no preamble or commentary."""


class OcrError(Exception):
    """A PDF could not be transcribed to usable full text and a retry would not help.

    The model refused, or the transcription was truncated at the token ceiling — both deterministic
    for the same PDF and model, so re-running only re-bills the model. The producer records this as a
    terminal FAILED outcome instead of retrying. Distinct from a transient API/network error (which
    the SDK's own exceptions carry and the producer lets propagate to be retried).
    """


@dataclasses.dataclass(frozen=True)
class OcrRendering:
    """A PDF's LLM-OCR transcription and the model that produced it.

    Attributes:
        markdown: The transcribed markdown text (may be blank — the caller treats a blank
            transcription as "no full text", not a rendering).
        model: The Anthropic model id the API reported, recorded as `Rendering.model`.
    """

    markdown: str
    model: str


async def convert_pdf(pdf_bytes: bytes) -> OcrRendering:
    """Transcribe a research-paper PDF to markdown via an Anthropic vision model.

    Args:
        pdf_bytes: Raw PDF bytes. The API caps the request body at 32 MB and the document at 600
            pages (100 on 200K-context models); an over-limit PDF is a loud `BadRequestError`.

    Returns:
        The transcription and the model id that produced it.

    Raises:
        anthropic.BadRequestError: The PDF exceeds the API's page or size limits (HTTP 400/413).
        OcrError: The generation was truncated at the token ceiling or refused by the model — either
            yields an incomplete transcription that must not be committed as full text, and a retry
            would not change the outcome.
    """
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('ascii')
    # AsyncAnthropic() reads its credentials from the environment (in the deployed worker, the
    # workload-identity-federation ids — docs/runbooks/claude-api-wif.md); the client holds an httpx
    # connection pool, so it is closed with the request rather than left to the GC.
    async with (
        anthropic.AsyncAnthropic(timeout=_TIMEOUT_SECONDS, max_retries=0) as client,
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
                        {'type': 'text', 'text': _INSTRUCTION},
                    ],
                }
            ],
        ) as stream,
    ):
        message = await stream.get_final_message()

    if message.stop_reason == 'max_tokens':
        raise OcrError(f'PDF transcription truncated at max_tokens={_MAX_TOKENS}')
    if message.stop_reason == 'refusal':
        raise OcrError('model refused to transcribe the PDF')

    markdown = ''.join(block.text for block in message.content if block.type == 'text')
    return OcrRendering(markdown=markdown, model=message.model)
