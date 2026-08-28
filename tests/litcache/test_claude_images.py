"""Tests for the Claude image-resize rule the page rasters are sized by.

The oracle is Anthropic's own documented worked examples: their reference implementation is what
decides what the model actually reads, so agreeing with it is the property under test, not the
shape of the code that computes it.
"""

from __future__ import annotations

import pytest

from themis.litcache import claude_images

_STANDARD = {'max_edge': 1568, 'max_tokens': 1568}
_HIGH = {'max_edge': 2576, 'max_tokens': 4784}


@pytest.mark.parametrize(
    ('size', 'expected'),
    [
        # Two of Anthropic's published worked examples, on the standard tier.
        pytest.param((1075, 1520), (924, 1307), id='a4-at-130dpi'),
        pytest.param((1920, 1080), (1456, 819), id='1080p'),
    ],
)
def test_matches_the_published_worked_examples(size: tuple[int, int], expected: tuple[int, int]) -> None:
    assert claude_images.resized_size(*size, **_STANDARD) == expected


def test_the_token_budget_binds_before_the_edge_limit() -> None:
    # The documented trap: a page under the edge limit on both sides is still resized, because its
    # patch count exceeds the budget, so what it reads is not what was sent.
    assert max(1075, 1520) < _STANDARD['max_edge']
    assert claude_images.count_image_tokens(1075, 1520) > _STANDARD['max_tokens']
    assert claude_images.resized_size(1075, 1520, **_STANDARD) != (1075, 1520)


def test_the_high_resolution_tier_reads_that_page_unresized() -> None:
    # Same page, larger budget: nothing is resized, so the page is read exactly as rendered.
    assert claude_images.resized_size(1075, 1520, **_HIGH) == (1075, 1520)


@pytest.mark.parametrize('size', [(4000, 3000), (800, 6000), (2479, 3508), (100, 100)])
def test_the_result_always_fits_the_limits_it_was_given(size: tuple[int, int]) -> None:
    # The invariant the search exists to establish, whatever the aspect ratio.
    width, height = claude_images.resized_size(*size, **_HIGH)
    assert claude_images.count_image_tokens(width, height) <= _HIGH['max_tokens']
    assert max(width, height) <= _HIGH['max_edge']


@pytest.mark.parametrize('size', [(4000, 3000), (800, 6000), (2479, 3508)])
def test_resizing_preserves_the_aspect_ratio(size: tuple[int, int]) -> None:
    # A stretched raster distorts every glyph on the page.
    width, height = claude_images.resized_size(*size, **_HIGH)
    assert width / height == pytest.approx(size[0] / size[1], rel=0.01)


def test_an_image_within_the_limits_is_left_alone() -> None:
    assert claude_images.resized_size(200, 200, **_STANDARD) == (200, 200)
