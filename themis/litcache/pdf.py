"""Probe a pdf source: recoverable text layer, and any embedded DOI.

A `pdf-derived` rendering reconstructs structure from the pdf, but recovering a
source quote's bounding box in that rendering needs the pdf's character layer:
positioned glyphs the extractor can map a quote back to. An image-only pdf
(scanned page, rasterized text) has no such layer, so quote→bbox is infeasible
and the paper is flagged for follow-up.

This module records `has_text_layer` per pdf source (a diagnostic only), and
harvests a DOI from the pdf's embedded metadata (`doi_from_metadata`) — a
publisher-populated id that identity uses when the bucket key and Docling origin
carry no external scheme (e.g. an Elsevier deposit keyed only by its PII). The DOI
is read from the XMP packet (`prism:doi` / `dc:identifier` — the authoritative
publisher field) in preference to a document-info string, and never from the body
text (a body match is as likely to be a cited reference as the work's own id).
XML-backed papers map a quote to the XML, so the text-layer probe does not apply to
them; the caller omits it when XML is the source of truth (`Source.has_text_layer`
stays unset).
"""

from __future__ import annotations

import contextlib
import re

import pypdfium2

# A pdf has a text layer if pypdfium2 recovers at least one positioned glyph
# across its pages. Image-only pdfs (text drawn as pixels, no text operators)
# yield zero; any genuine text layer yields a positive count.
_MIN_TEXT_CHARS = 1

# A DOI in a document-info string (e.g. an Elsevier `Subject`: "…doi:10.1016/j.scr.2025.103712").
_DOI_RE = re.compile(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+')

# The document-info keys a publisher embeds a DOI in, in preference order.
_INFO_DOI_KEYS = ('DOI', 'Subject', 'Title', 'Keywords')

# The XMP metadata tags carrying the DOI, in preference order. `prism:doi` is the
# journal-DOI field; `dc:identifier` is more general (its value may be prefixed
# `doi:`). The packet is a plaintext XML stream in the pdf, so it is matched against
# the raw bytes — scoped to these tag names, not a DOI anywhere in the file.
_XMP_DOI_TAGS = (b'prism:doi', b'dc:identifier')


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


def doi_from_metadata(pdf_bytes: bytes) -> str | None:
    """Harvest a DOI from the pdf's embedded metadata, if one is present.

    Identity enrichment for id-poor deposits: publishers populate the DOI into the
    XMP packet (`prism:doi` / `dc:identifier`) and often a document-info field, so a
    deposit whose only external id is an opaque PII still carries its DOI here. XMP
    is preferred (authoritative); the document-info fields are the fallback.

    Args:
        pdf_bytes: The raw pdf source bytes.

    Returns:
        The embedded DOI (bare, no `doi:` prefix), or None when the pdf has no bytes
        or carries no DOI in either place.

    Raises:
        pypdfium2.PdfiumError: If the (non-empty) bytes are not a loadable pdf and no
            XMP DOI was found (the document-info fallback loads the pdf).
    """
    if not pdf_bytes:  # a degenerate empty source has no metadata to read
        return None
    return _doi_from_xmp(pdf_bytes) or _doi_from_info_dict(pdf_bytes)


def _doi_from_xmp(pdf_bytes: bytes) -> str | None:
    """Read a DOI from the XMP packet's `prism:doi` / `dc:identifier`, if present."""
    for tag in _XMP_DOI_TAGS:
        # Element (`>value`) or attribute (`="value"`) form; an optional `doi:` prefix
        # (dc:identifier). Anchored to the tag, so a body-text DOI can't match.
        match = re.search(tag + rb'\s*[>=]\s*["\']?(?:doi:)?\s*(10\.\d{4,9}/[^\s"\'<]+)', pdf_bytes)
        if match is not None:
            return match.group(1).rstrip(b'.').decode('ascii', 'ignore')
    return None


def _doi_from_info_dict(pdf_bytes: bytes) -> str | None:
    """Read a DOI from the pdf document-info fields (`_INFO_DOI_KEYS`), if present."""
    with pypdfium2.PdfDocument(pdf_bytes) as doc:
        for key in _INFO_DOI_KEYS:
            value = doc.get_metadata_value(key)
            match = _DOI_RE.search(value) if value else None
            if match is not None:
                return match.group(0).rstrip('.')
    return None


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
