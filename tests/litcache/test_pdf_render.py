"""Tests for pdf page rasterization.

The raster is what a model transcribes from, so what matters is that the pixel size asked for is the
pixel size produced, and that it is reported alongside the image.
"""

from __future__ import annotations

import io

import PIL.Image
import pypdfium2
import pytest

from themis.litcache import pdf


def _pdf(pages: list[tuple[int, int]]) -> bytes:
    """A pdf with one blank page per `(width, height)` in points."""
    document = pypdfium2.PdfDocument.new()
    for width, height in pages:
        document.new_page(width, height)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_a_page_is_rendered_at_the_size_the_fit_asks_for() -> None:
    # The whole point of the seam: a provider that shrinks anything larger reads the shrunken image,
    # so the raster has to be exactly what it would have chosen.
    rendered = pdf.render_pages(_pdf([(612, 792)]), fit=lambda _w, _h: (850, 1100))

    assert [(page.width, page.height) for page in rendered] == [(850, 1100)]


def test_every_page_is_rendered_and_numbered_from_one() -> None:
    # The number is what a model names a page by, so an off-by-one misplaces everything it says
    # about one.
    rendered = pdf.render_pages(_pdf([(612, 792), (612, 792), (300, 300)]), fit=lambda _w, _h: (100, 100))

    assert [page.number for page in rendered] == [1, 2, 3]


def test_the_fit_is_given_the_page_size_in_points() -> None:
    # A provider sizes from the aspect ratio, so it has to see the page's own shape, not a raster.
    seen: list[tuple[float, float]] = []

    def record(width: float, height: float) -> tuple[int, int]:
        seen.append((width, height))
        return (100, 100)

    pdf.render_pages(_pdf([(612, 792), (300, 900)]), fit=record)

    assert seen == [(612, 792), (300, 900)]


def test_the_raster_is_a_png() -> None:
    (page,) = pdf.render_pages(_pdf([(612, 792)]), fit=lambda _w, _h: (100, 100))

    assert page.png.startswith(b'\x89PNG\r\n\x1a\n')


def test_page_count_matches_what_is_rendered() -> None:
    document = _pdf([(612, 792), (612, 792)])

    assert pdf.page_count(document) == len(pdf.render_pages(document, fit=lambda _w, _h: (50, 50)))


def test_a_fit_that_returns_no_pixels_fails_loudly() -> None:
    # A zero-sized raster carries no page at all, which is a broken request rather than a small one.
    with pytest.raises(ValueError, match='0x100'):
        pdf.render_pages(_pdf([(612, 792)]), fit=lambda _w, _h: (0, 100))


def test_the_image_is_the_page_and_nothing_else() -> None:
    # Nothing is drawn onto or above the raster: the page is identified by the text part that
    # precedes it in the request, so the image is exactly the pixels the fit asked for.
    (page,) = pdf.render_pages(_pdf([(612, 792)]), fit=lambda _w, _h: (600, 800))

    image = PIL.Image.open(io.BytesIO(page.png)).convert('RGB')
    assert image.size == (page.width, page.height) == (600, 800)
    assert image.getcolors() == [(600 * 800, (255, 255, 255))]
