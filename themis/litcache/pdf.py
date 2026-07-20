"""Probe whether a pdf source has a recoverable text layer.

A `pdf-derived` rendering reconstructs structure from the pdf, but recovering a
source quote's bounding box in that rendering needs the pdf's character layer:
positioned glyphs the extractor can map a quote back to. An image-only pdf
(scanned page, rasterized text) has no such layer, so quote→bbox is infeasible
and the paper is flagged for follow-up.

This module records `has_text_layer` per pdf source. It is a diagnostic only.
XML-backed papers map a quote to the XML, so the probe does not apply to them; the
caller omits it when XML is the source of truth (`Source.has_text_layer` stays unset).
"""

from __future__ import annotations

import contextlib

import pypdfium2

# A pdf has a text layer if pypdfium2 recovers at least one positioned glyph
# across its pages. Image-only pdfs (text drawn as pixels, no text operators)
# yield zero; any genuine text layer yields a positive count.
_MIN_TEXT_CHARS = 1


def probe_has_text_layer(pdf_bytes: bytes) -> bool:
    """Whether a pdf carries a recoverable text layer.

    Args:
        pdf_bytes: The raw pdf source bytes.

    Returns:
        True if pypdfium2 recovers any positioned characters (quote→bbox
        feasible); False for an image-only pdf with no text layer.

    Raises:
        pypdfium2.PdfiumError: If the bytes are not a loadable pdf.
    """
    return _count_chars(pdf_bytes) >= _MIN_TEXT_CHARS


def _count_chars(pdf_bytes: bytes) -> int:
    """Total positioned characters across all pages of the pdf."""
    total = 0
    with pypdfium2.PdfDocument(pdf_bytes) as doc:
        for page in doc:
            with contextlib.closing(page):
                textpage = page.get_textpage()
                with contextlib.closing(textpage):
                    total += textpage.count_chars()
    return total
