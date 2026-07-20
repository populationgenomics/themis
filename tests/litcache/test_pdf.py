"""Tests for `themis.litcache.pdf` over the committed pdf fixtures.

The text-layer probe distinguishes a pdf with a recoverable text layer
(`nonoa/source.pdf`) from an image-only pdf (`image_only/source.pdf`, text drawn
as pixels).
"""

from __future__ import annotations

import pathlib

import pypdfium2
import pytest

from themis.litcache import pdf

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_TEXT_PDF = _FIXTURES / 'nonoa' / 'source.pdf'
_IMAGE_ONLY_PDF = _FIXTURES / 'image_only' / 'source.pdf'


def test_text_pdf_has_text_layer() -> None:
    assert pdf.probe_has_text_layer(_TEXT_PDF.read_bytes()) is True


def test_image_only_pdf_has_no_text_layer() -> None:
    assert pdf.probe_has_text_layer(_IMAGE_ONLY_PDF.read_bytes()) is False


def test_malformed_pdf_fails_loud() -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        pdf.probe_has_text_layer(b'not a pdf')
