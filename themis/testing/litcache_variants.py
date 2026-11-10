"""Stand-ins for the variant steps' collaborators, for tests of the worker loop that never reach them."""

from __future__ import annotations

from themis.litcache import produce, variant_extract
from themis.litcache.models import variants_pb2

VALIDATION = variants_pb2.VariantValidationConstruction(
    weaver_version='0.0.0',
    stores=[variants_pb2.StoreVersion(name='refseq-grch38', release='test')],
    validator_version='0',
)


async def _extract(_markdown: str) -> variant_extract.Extraction:
    raise AssertionError('the variant extraction must not be called')


def _validate(
    _mention_set: variants_pb2.MentionSet, _text: str
) -> tuple[variants_pb2.MentionSet, variants_pb2.Verdicts, variants_pb2.VariantValidationConstruction]:
    raise AssertionError('the variant validation must not be called')


# Collaborators that refuse to be called.
UNUSED = produce.Variants(extract=_extract, validate=_validate, validation=VALIDATION)
