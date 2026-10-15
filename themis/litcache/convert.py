"""Render a seed paper's source to markdown — both conversion branches.

A paper's seed snapshot becomes one rendering via one of two branches:

- OA / xml-faithful (`convert_jats`): full-text XML fetched from the litfetch
  ladder (gated by `themis.litcache.oa`), converted with litdown. Higher-fidelity — the
  XML carries the publisher's own structure.
- non-OA / pdf-derived (`convert_docling`): the seed bucket's `DoclingDocument`
  rendered with docling-core's `export_to_markdown()`. Docling reconstructs
  structure from the pdf's layout, so it is lower-fidelity; this is the fallback
  when no OA XML is obtainable.

Both functions are pure, and say only how the markdown was made: which revision it
was made from and when are the caller's, which records them on the rendering's
record (`writer.RenderingInput`).
"""

from __future__ import annotations

import dataclasses
import importlib.metadata

import litdown
from docling_core.types.doc import document as docling_document

from themis.litcache.models import litcache_pb2

# The non-OA branch names the converter `docling`; its version is docling-core's,
# the package that owns export_to_markdown (pinned for rendering reproducibility).
_CONVERTER = litcache_pb2.Converter.CONVERTER_DOCLING
_CONVERTER_VERSION = importlib.metadata.version('docling-core')

# The OA branch names the converter `litdown`; its version is litdown's, the
# package that owns the JATS/Elsevier XML→markdown conversion.
_JATS_CONVERTER = litcache_pb2.Converter.CONVERTER_LITDOWN
_JATS_CONVERTER_VERSION = importlib.metadata.version('litdown')


@dataclasses.dataclass(frozen=True)
class Conversion:
    """A rendered markdown document and how it was made.

    Attributes:
        markdown: The rendered markdown text — the rendering's bytes before the
            caller writes them.
        construction: The converter and its version, for the rendering's record.
    """

    markdown: str
    construction: litcache_pb2.RenderingConstruction


def convert_docling(docling_json: bytes | str) -> Conversion:
    """Convert a Docling json document to a `pdf-derived` markdown rendering.

    Args:
        docling_json: The raw DoclingDocument json (bytes or str).

    Returns:
        The `Conversion`: the markdown text and its construction
        (`converter=docling`).

    Raises:
        pydantic.ValidationError: If `docling_json` is not a valid
            DoclingDocument.
        ValueError: If the export produces empty markdown.
    """
    doc = docling_document.DoclingDocument.model_validate_json(docling_json)
    markdown = doc.export_to_markdown()
    if not markdown.strip():
        raise ValueError(f'docling export produced empty markdown for {doc.name!r}')

    construction = litcache_pb2.RenderingConstruction(converter=_CONVERTER, converter_version=_CONVERTER_VERSION)
    return Conversion(markdown=markdown, construction=construction)


def convert_jats(jats_xml: bytes) -> Conversion:
    """Convert full-text XML to an `xml-faithful` markdown rendering.

    The OA branch: the XML body has been fetched from the litfetch ladder (see
    `themis.litcache.oa.fetch_oa_source`) and is converted with litdown, which sniffs the
    root element to dispatch between the dialects it renders: a JATS `<article>`, a BITS
    `<book-part-wrapper>` (a Bookshelf chapter), or Elsevier's retrieval response.

    Args:
        jats_xml: The full-text XML bytes (JATS, BITS, or Elsevier `ce:`/`ja:`).

    Returns:
        The `Conversion`: the markdown text and its construction
        (`converter=litdown`).

    Raises:
        ValueError: If the XML root is none litdown renders (it rejects one rather
            than returning empty), or the conversion produces empty markdown.
    """
    markdown = litdown.convert(jats_xml)
    if not markdown.strip():
        raise ValueError('litdown conversion produced empty markdown')

    construction = litcache_pb2.RenderingConstruction(
        converter=_JATS_CONVERTER, converter_version=_JATS_CONVERTER_VERSION
    )
    return Conversion(markdown=markdown, construction=construction)
