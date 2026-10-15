"""What a manifest's records and refs say: which rendering a reader gets, and on what terms.

A derived artifact is a `Record` in `Manifest.records`, keyed by the sha256 of its bytes; `Manifest.refs` names the
record of each kind a reader is served (`docs/design/litcache-manifest.md`). The writer moves a ref when it adds a
record that outranks the one named, the migration sets refs from the ranking, and readers follow them. All three read
the ranking and the term resolution from here, so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable

from themis.litcache.models import litcache_pb2

# The ref naming the rendering a reader of the paper's text is served.
MARKDOWN_REF = 'markdown'

# Rendering fidelity, best first: an xml-faithful litdown rendering over a pdf-derived one, and llm-ocr over the legacy
# docling route. A converter missing here ranks below every listed one rather than raising: during a deploy, a writer on
# the previous build can meet a record a newer build wrote, and refusing to rank it would strand the paper's write.
_CONVERTER_RANK = {
    litcache_pb2.Converter.CONVERTER_LITDOWN: 0,
    litcache_pb2.Converter.CONVERTER_LLM_OCR: 1,
    litcache_pb2.Converter.CONVERTER_DOCLING: 2,
}
_CONVERTER_RANK_MISS = max(_CONVERTER_RANK.values()) + 1


def _rendering_key(key: str, record: litcache_pb2.Record) -> tuple[int, int, int, str]:
    # Lower is better: converter fidelity, then the newer record, then the hash, a total order because protobuf-map
    # iteration order is unspecified.
    rank = _CONVERTER_RANK.get(record.rendering.converter, _CONVERTER_RANK_MISS)
    return (rank, -record.created_at.seconds, -record.created_at.nanos, key)


def is_rendering(record: litcache_pb2.Record) -> bool:
    """Whether `record` is a rendering: markdown a reader quotes from."""
    return record.WhichOneof('construction') == 'rendering'


def best_rendering(records: Iterable[tuple[str, litcache_pb2.Record]]) -> str | None:
    """The key of the highest-ranked rendering among `records`, or None when there is none.

    Ranks by converter fidelity, then the newer `created_at`, then the key.
    """
    renderings = [(key, record) for key, record in records if is_rendering(record)]
    if not renderings:
        return None
    return min(renderings, key=lambda item: _rendering_key(*item))[0]


def markdown(manifest: litcache_pb2.Manifest) -> tuple[str, litcache_pb2.Record] | None:
    """The rendering the `markdown` ref names, as `(key, record)`, or None when the paper has no text.

    Raises:
        ValueError: If the ref names a record the manifest does not hold, or one that is not a rendering. The
            manifest's own constraints forbid the first, so either is a corrupt manifest.
    """
    key = manifest.refs.get(MARKDOWN_REF)
    if key is None:
        return None
    record = manifest.records.get(key)
    if record is None or not is_rendering(record):
        raise ValueError(
            f'manifest {manifest.doc_id}: the markdown ref names {key!r}, which is not a rendering it holds'
        )
    return key, record


def source_revisions(
    manifest: litcache_pb2.Manifest, record: litcache_pb2.Record
) -> list[tuple[litcache_pb2.Source, litcache_pb2.Revision]]:
    """Each `(lineage, revision)` a record's deps name directly, across every lineage holding each hash."""
    found: list[tuple[litcache_pb2.Source, litcache_pb2.Revision]] = []
    for dep in record.deps:
        for source in manifest.sources:
            found.extend((source, r) for r in source.revisions if r.hash == dep)
    return found
