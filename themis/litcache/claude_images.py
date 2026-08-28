"""The size Claude resizes an image to, so a page is read at the size it was rendered at.

Claude sees an image in 28x28 patches and shrinks anything past its limits before reading it. A
caller who renders a page at its own choice of size never learns what that shrink did to it — an
unasked-for resample, softening exactly the thin strokes and small glyphs a dense page is
transcribed from. Rendering each page at the size Claude would have chosen is what makes the image
it reads the image we produced.

`resized_size` is Anthropic's reference implementation. The rule is not a long-edge cap — a 1920x1080
page resizes to 1456x819, not 1568x882 — but the largest aspect-preserving size satisfying both the
edge limit and the visual-token budget, which only a search finds.
"""

from __future__ import annotations

import math
from typing import NamedTuple

# Claude reads an image as 28x28 patches; one patch is one visual token.
PATCH = 28


class Tier(NamedTuple):
    """The limits one class of model reads images under.

    Attributes:
        max_edge: The longest either side may be.
        max_tokens: The visual-token budget one image may cost.
    """

    max_edge: int
    max_tokens: int


# Claude 4.7 and later read at the high-resolution tier; everything earlier at the standard one. The
# pair is load-bearing together: passing one tier's limits for a model on the other recovers the
# wrong size, and the image is resized on arrival after all.
HIGH_RESOLUTION = Tier(max_edge=2576, max_tokens=4784)
STANDARD_RESOLUTION = Tier(max_edge=1568, max_tokens=1568)

# Past 20 image blocks in one request, every image in it must also stay under this on each side or
# the request is rejected outright. A paper of more than twenty pages is one such request.
MANY_IMAGE_MAX_EDGE = 2000
MANY_IMAGE_THRESHOLD = 20

# One request carries at most this many image blocks, and one image at most this many base64 bytes on
# the wire. Both are rejections, not resizes. The block ceiling is the one for a 200k-context model —
# 600 is for models above that, and taking the larger number builds a request the API refuses. The
# byte ceiling is the first-party API's; Bedrock and Vertex hold a model to 5 MB.
MAX_IMAGE_BLOCKS = 100
MAX_IMAGE_BASE64_BYTES = 10 * 1024 * 1024


def count_image_tokens(width: int, height: int) -> int:
    """Visual tokens an image costs: one per 28x28 patch."""
    return math.ceil(width / PATCH) * math.ceil(height / PATCH)


def resized_size(width: int, height: int, *, max_edge: int, max_tokens: int) -> tuple[int, int]:
    """The size Claude resizes an image to before padding.

    Args:
        width: The image's width in pixels.
        height: The image's height in pixels.
        max_edge: The tier's maximum edge length.
        max_tokens: The tier's visual-token budget.

    Returns:
        The `(width, height)` Claude reads the image at, unchanged when it already fits.
    """

    def fits(w: int, h: int) -> bool:
        return (
            math.ceil(w / PATCH) * PATCH <= max_edge
            and math.ceil(h / PATCH) * PATCH <= max_edge
            and count_image_tokens(w, h) <= max_tokens
        )

    if fits(width, height):
        return (width, height)
    if height > width:
        resized_h, resized_w = resized_size(height, width, max_edge=max_edge, max_tokens=max_tokens)
        return (resized_w, resized_h)

    aspect_ratio = width / height
    lo, hi = 1, width  # lo always fits; hi never fits
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / aspect_ratio), 1)):
            lo = mid
        else:
            hi = mid
    return (lo, max(round(lo / aspect_ratio), 1))
