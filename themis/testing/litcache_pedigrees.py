"""Stand-ins for the figure and pedigree steps' collaborators, for tests of the worker loop that never reach them."""

from __future__ import annotations

from collections.abc import Sequence

from themis.litcache import figure_annotate, pdf, pedigree_extract, produce


async def _annotate(_pdf: bytes, _layouts: Sequence[pdf.PageLayout]) -> figure_annotate.Annotation:
    raise AssertionError('the figure annotation must not be called')


async def _read(_paper: pedigree_extract.PaperInput) -> pedigree_extract.Reading:
    raise AssertionError('the pedigree reading must not be called')


# Collaborators that refuse to be called.
UNUSED_FIGURES = produce.Figures(annotate=_annotate)
UNUSED = produce.Pedigrees(read=_read)
