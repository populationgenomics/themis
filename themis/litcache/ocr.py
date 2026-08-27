"""The vocabulary every PDF→markdown converter shares: its result, its failure, its prompt.

A converter transcribes a research paper's PDF to markdown when the OA ladder served no usable XML
(`docs/design/evidence-fulltext.md`). Each provider lives in its own module (`anthropic_ocr`); this
one holds what they must agree on, so no provider imports another.

`INSTRUCTION` is shared deliberately rather than per-provider: holding the prompt constant is what
makes a transcription difference attributable to the model.

`Rendering.converter_version` is `{PROMPT_VERSION}.{provider harness}` — the shared ask, then the
provider's own request shape. Both are judgements, not derivations: transcription is stochastic, so
no version can promise that two renderings sharing one are alike. They mark "produced before we
changed something substantive", which is what a re-render sweep selects on. Bump `PROMPT_VERSION` for
a changed prompt, which supersedes every provider's renderings at once; bump a provider's own for its
model or request shape. The minor is a per-provider counter, so it orders only within a provider —
selecting one provider's renderings means filtering on `Rendering.model` too.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable

PROMPT_VERSION = '1'

INSTRUCTION = r"""Carefully transcribe this research-paper PDF to GitHub-flavored Markdown. The output is
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

    The provider determined this PDF will not transcribe, however many times it is asked — a refusal,
    a truncation at the token ceiling, a content block. Deterministic for the same PDF and model, so
    re-running only re-bills it, and the producer records a terminal FAILED outcome instead of
    retrying. Each provider's `Raises:` names the conditions it maps here. Distinct from a transient
    API/network error, which a provider lets propagate to be retried.
    """


@dataclasses.dataclass(frozen=True)
class OcrRendering:
    """A PDF's LLM-OCR transcription and the model that produced it.

    Attributes:
        markdown: The transcribed markdown text (may be blank — the caller treats a blank
            transcription as "no full text", not a rendering).
        model: The model id the provider reported for the call that produced these bytes, recorded
            as `Rendering.model`.
        converter_version: The provider's `converter_version(...)`, recorded as
            `Rendering.converter_version`.
    """

    markdown: str
    model: str
    converter_version: str


def converter_version(harness_version: str) -> str:
    """Compose the recorded `Rendering.converter_version` from a provider's own harness version."""
    return f'{PROMPT_VERSION}.{harness_version}'


PdfConverter = Callable[[bytes], Awaitable[OcrRendering]]
"""The injected transcriber. A provider whose `convert_pdf` takes configuration is bound to this
shape by the caller that supplies it."""
