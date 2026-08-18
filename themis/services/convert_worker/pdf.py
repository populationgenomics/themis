"""The conversion lane's PDF branch, before a model backend is wired.

`litcache.produce.produce_full_text` tries the OA ladder first and falls back to transcribing the
paper's PDF. This module supplies that fallback. With no model backend configured it raises, and
raises something that is *not* `ocr.OcrError`: the producer records a terminal marker for that class
alone, so this propagates, the worker returns 5xx, Cloud Tasks retries, and the paper stays PENDING.

Settling the paper instead would be wrong in a way nothing downstream could recover from. A terminal
marker short-circuits `produce_full_text` before it re-walks the OA ladder, and litfetch reports a
transient fetch failure and an absent body identically — so a paper whose XML was always there would
be written off permanently on the strength of our own missing configuration.
"""

from __future__ import annotations

from themis.litcache import ocr


class ConverterUnconfiguredError(RuntimeError):
    """The PDF branch was reached with no model backend wired."""


async def unconfigured_convert_pdf(pdf_bytes: bytes) -> ocr.OcrRendering:
    """Fail the conversion, leaving the paper PENDING.

    Args:
        pdf_bytes: The paper's PDF, unread — there is no converter to hand it to.

    Raises:
        ConverterUnconfiguredError: Always.
    """
    del pdf_bytes
    raise ConverterUnconfiguredError('no PDF converter is configured')
