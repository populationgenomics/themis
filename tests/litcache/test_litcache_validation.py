"""Guard the litcache at-rest invariants declared as protovalidate options (proto.md).

access-iff-publisher is structural (``Access`` is a ``oneof``, ``publisher`` exists only on
``licensed``); the residual rules — access present, a lineage's non-empty revisions, a
rendering's ``model`` set iff its converter is ``llm_ocr`` — are protovalidate options
enforced by ``protovalidate.validate``.
"""

from __future__ import annotations

import protovalidate
import pytest
from pubmed_proto import pubmed_pb2

from themis.litcache import paper_metadata
from themis.litcache.models import crossref_pb2, litcache_pb2


def _source(
    access: litcache_pb2.Access | None = None,
    revisions: list[litcache_pb2.Revision] | None = None,
) -> litcache_pb2.Source:
    if access is None:
        access = litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead())
    if revisions is None:
        revisions = [litcache_pb2.Revision(hash='abc', kind=litcache_pb2.SOURCE_KIND_SEED)]
    return litcache_pb2.Source(
        handle='pdf',
        media_type=litcache_pb2.SOURCE_FORMAT_PDF,
        licence='cc-by',
        licence_basis=litcache_pb2.LICENCE_BASIS_ARTIFACT,
        access=access,
        revisions=revisions,
    )


@pytest.mark.parametrize(
    'access',
    [
        litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        litcache_pb2.Access(licensed=litcache_pb2.Licensed(publisher='Elsevier')),
        litcache_pb2.Access(institution_captured=litcache_pb2.InstitutionCaptured()),
        litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
    ],
)
def test_valid_source_passes(access: litcache_pb2.Access) -> None:
    protovalidate.validate(_source(access=access))  # does not raise


def test_absent_access_fails() -> None:
    source = _source()
    source.ClearField('access')
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(source)


def test_empty_access_oneof_fails() -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(_source(access=litcache_pb2.Access()))


def test_licensed_without_publisher_fails() -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(_source(access=litcache_pb2.Access(licensed=litcache_pb2.Licensed())))


def test_empty_revisions_fails() -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(_source(revisions=[]))


def _rendering(
    converter: litcache_pb2.Converter = litcache_pb2.CONVERTER_LITDOWN,
    model: str | None = None,
) -> litcache_pb2.Rendering:
    rendering = litcache_pb2.Rendering(
        from_source='pdf',
        from_revision='abc',
        converter=converter,
        converter_version='0.4',
    )
    if model is not None:
        rendering.model = model
    return rendering


def test_rendering_model_iff_llm_ocr() -> None:
    protovalidate.validate(_rendering())  # litdown, no model
    protovalidate.validate(_rendering(converter=litcache_pb2.CONVERTER_LLM_OCR, model='claude-opus-4-8'))
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(_rendering(model='claude-opus-4-8'))  # model on non-llm_ocr
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(_rendering(converter=litcache_pb2.CONVERTER_LLM_OCR))  # llm_ocr without model


def _journal_record() -> litcache_pb2.PubmedRecord:
    return litcache_pb2.PubmedRecord(article=pubmed_pb2.PubmedArticle())


_ENVELOPES_HOLDING_A_RECORD = [
    litcache_pb2.PaperMetadata(pubmed=_journal_record()),
    litcache_pb2.PaperMetadata(pubmed=litcache_pb2.PubmedRecord(book_article=pubmed_pb2.PubmedBookArticle())),
    litcache_pb2.PaperMetadata(crossref=crossref_pb2.Work(doi='10.1/x')),
    litcache_pb2.PaperMetadata(pubmed=_journal_record(), crossref=crossref_pb2.Work(doi='10.1/x')),
]
_ENVELOPES_HOLDING_NO_RECORD = [
    litcache_pb2.PaperMetadata(),
    litcache_pb2.PaperMetadata(pubmed=litcache_pb2.PubmedRecord()),
]


@pytest.mark.parametrize('envelope', _ENVELOPES_HOLDING_A_RECORD)
def test_an_envelope_holding_a_record_is_valid(envelope: litcache_pb2.PaperMetadata) -> None:
    protovalidate.validate(envelope)  # does not raise


@pytest.mark.parametrize('envelope', _ENVELOPES_HOLDING_NO_RECORD)
def test_an_envelope_holding_no_record_is_invalid(envelope: litcache_pb2.PaperMetadata) -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(envelope)


@pytest.mark.parametrize('envelope', _ENVELOPES_HOLDING_A_RECORD + _ENVELOPES_HOLDING_NO_RECORD)
def test_the_code_check_agrees_with_the_declared_constraints(envelope: litcache_pb2.PaperMetadata) -> None:
    # The proto declares the constraints; `paper_metadata.check` enforces them at the boundary in
    # code. Both must settle every envelope the same way, or the declaration and the enforcement
    # have drifted.
    try:
        protovalidate.validate(envelope)
    except protovalidate.ValidationError:
        with pytest.raises(ValueError, match=r'no record set|neither of its kinds'):
            paper_metadata.check(envelope)
    else:
        paper_metadata.check(envelope)  # does not raise
