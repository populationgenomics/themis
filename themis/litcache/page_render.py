"""Rendering a region of a pdf page as an image a Claude model reads at full size.

The model shrinks any image past its tier before reading it, so a region is rendered at the largest size the tier
takes unshrunk, and never finer than pdfium resolves: past that a render adds pixels but no detail.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import io

import anthropic
import PIL.Image

from themis.litcache import claude_images, pdf

# pdfium renders no finer than this; a region rendered past it adds no detail.
MAX_DPI = 1200
_POINTS_PER_INCH = 72


@dataclasses.dataclass(frozen=True)
class Image:
    """A rendered region, at the size the model reads it.

    Attributes:
        png: The encoded image.
        width: Its width in pixels.
        height: Its height in pixels.
        native_limited: The region was rendered at pdfium's finest, short of the tier's size: zooming further into it
            adds no detail.
    """

    png: bytes
    width: int
    height: int
    native_limited: bool = False

    @property
    def wire_bytes(self) -> int:
        """Its size as base64 in a request."""
        return (len(self.png) + 2) // 3 * 4

    def block(self) -> anthropic.types.ImageBlockParam:
        """It as an image block of a request."""
        data = base64.standard_b64encode(self.png).decode('ascii')
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}}


def _fit(width: float, height: float, tier: claude_images.Tier) -> tuple[int, int]:
    """The largest size an image of this shape reaches the model at, unresized."""
    # Claude only shrinks, so the shape starts well past the tier and the tier search brings it down.
    scale = 4 * tier.max_edge / max(width, height)
    return claude_images.resized_size(
        max(1, round(width * scale)),
        max(1, round(height * scale)),
        max_edge=tier.max_edge,
        max_tokens=tier.max_tokens,
    )


def _within_tier(image: pdf.PageImage, tier: claude_images.Tier) -> Image:
    """The rendering, shrunk to the size the model reads it at when pdfium's rounding made it a pixel too large."""
    fitted = claude_images.resized_size(image.width, image.height, max_edge=tier.max_edge, max_tokens=tier.max_tokens)
    if fitted == (image.width, image.height):
        return Image(image.png, image.width, image.height)
    with PIL.Image.open(io.BytesIO(image.png)) as opened:
        resized = opened.resize(fitted, PIL.Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format='PNG')
    return Image(buffer.getvalue(), fitted[0], fitted[1])


async def render(
    pdf_bytes: bytes,
    layout: pdf.PageLayout,
    box: tuple[float, float, float, float],
    *,
    tier: claude_images.Tier,
) -> Image:
    """A region of a page, as fractions of the page from its top-left corner, at the most detail the tier takes.

    Args:
        pdf_bytes: The pdf.
        layout: The page's layout, for its size in points.
        box: The region, `(left, top, right, bottom)`, each in 0..1.
        tier: The image tier of the model that reads it.

    Raises:
        ValueError: The region is too small to render, or pdfium refuses it.
        pypdfium2.PdfiumError: pdfium cannot load the pdf, or crashed.
    """
    width = (box[2] - box[0]) * layout.width
    height = (box[3] - box[1]) * layout.height
    wanted = max(_fit(width, height, tier))
    finest = round(max(width, height) * MAX_DPI / _POINTS_PER_INCH)
    if finest < 1:
        raise ValueError(f'the region {box} is too small to render')
    image = await asyncio.wrap_future(
        pdf.render_region(pdf_bytes, page=layout.number, box=box, max_edge=min(wanted, finest))
    )
    fitted = await asyncio.to_thread(_within_tier, image, tier)
    return dataclasses.replace(fitted, native_limited=finest < wanted)
