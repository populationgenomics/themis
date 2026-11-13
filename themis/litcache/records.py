"""What a manifest's records and refs say, and the one rule every writer adds a record by.

A derived artifact is a `Record` in `Manifest.records`, keyed by the sha256 of its bytes; `Manifest.refs` names the
record of each kind a reader is served (`docs/design/litcache-manifest.md`). Every writer adds a record through
`put_record`, which also moves the ref the record competes for when it outranks the one named, and readers follow the
refs. Each kind's ranking lives here, beside that rule.
"""

from __future__ import annotations

from collections.abc import Callable

from themis.litcache.models import litcache_pb2

# The ref naming the rendering a reader of the paper's text is served.
MARKDOWN_REF = 'markdown'
# The ref naming the variant mention set the extraction agent wrote from the paper.
VARIANTS_REF = 'variants'
# The ref naming weaver's verdicts on the mention set the `variants` ref names.
VERDICTS_REF = 'verdicts'
# The ref naming the paper's figure set: where each figure of its pdf is, and what kind each panel is.
FIGURES_REF = 'figures'
# The ref naming the paper's pedigree set: every pedigree read from its figures.
PEDIGREES_REF = 'pedigrees'

# Rendering fidelity, best first: an xml-faithful litdown rendering over a pdf-derived one, and llm-ocr over the legacy
# docling route. A converter missing here ranks below every listed one rather than raising: during a deploy, a writer on
# the previous build can meet a record a newer build wrote, and refusing to rank it would strand the paper's write.
_CONVERTER_RANK = {
    litcache_pb2.Converter.CONVERTER_LITDOWN: 0,
    litcache_pb2.Converter.CONVERTER_LLM_OCR: 1,
    litcache_pb2.Converter.CONVERTER_DOCLING: 2,
}
_CONVERTER_RANK_MISS = max(_CONVERTER_RANK.values()) + 1


def _rendering_key(
    _manifest: litcache_pb2.Manifest, key: str, record: litcache_pb2.Record
) -> tuple[int, int, int, str]:
    # Lower is better: converter fidelity, then the newer record, then the hash, a total order because protobuf-map
    # iteration order is unspecified.
    rank = _CONVERTER_RANK.get(record.rendering.converter, _CONVERTER_RANK_MISS)
    return (rank, -record.created_at.seconds, -record.created_at.nanos, key)


def _made_from(
    *upstreams: str,
    optional: tuple[str, ...] = (),
) -> Callable[[litcache_pb2.Manifest, str, litcache_pb2.Record], tuple[int, int, int, str]]:
    """A derived kind's ranking: made from what each of `upstreams` names now first, then the newest, then the hash.

    An `optional` upstream counts only while its ref is set: a record made without it is current when there is none.

    Recency alone is not enough: a slow run on an input since superseded can finish after the run on its successor.
    """

    def rank(manifest: litcache_pb2.Manifest, key: str, record: litcache_pb2.Record) -> tuple[int, int, int, str]:
        current = all(ref in manifest.refs and manifest.refs[ref] in record.deps for ref in upstreams) and all(
            ref not in manifest.refs or manifest.refs[ref] in record.deps for ref in optional
        )
        return (0 if current else 1, -record.created_at.seconds, -record.created_at.nanos, key)

    return rank


def _newest_pdf(manifest: litcache_pb2.Manifest) -> str | None:
    """The hash of the most recently captured revision of any pdf lineage, or None for a paper with no pdf."""
    revisions = [
        r for s in manifest.sources if s.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF for r in s.revisions
    ]
    if not revisions:
        return None
    return max(revisions, key=lambda r: r.captured_at.ToDatetime()).hash


def _made_from_newest_pdf(
    manifest: litcache_pb2.Manifest, key: str, record: litcache_pb2.Record
) -> tuple[int, int, int, str]:
    """A kind made from a pdf: from the newest pdf revision first, then the newest, then the hash."""
    newest = _newest_pdf(manifest)
    current = newest is not None and newest in record.deps
    return (0 if current else 1, -record.created_at.seconds, -record.created_at.nanos, key)


# Each kind's ranking, by its construction arm: a sort key, lower is better. A kind with none can be held but not
# named by a ref.
_RANKINGS: dict[str, Callable[[litcache_pb2.Manifest, str, litcache_pb2.Record], tuple[int, int, int, str]]] = {
    'rendering': _rendering_key,
    'variant_extraction': _made_from(MARKDOWN_REF),
    'variant_validation': _made_from(VARIANTS_REF),
    'figure_annotation': _made_from_newest_pdf,
    'pedigree_extraction': _made_from(MARKDOWN_REF, FIGURES_REF, optional=(VERDICTS_REF,)),
}


class RecordRefusedError(ValueError):
    """A record or ref the manifest cannot take, on any attempt: a corrupt ref, or bytes claimed by two kinds."""


def _newer(record: litcache_pb2.Record, than: litcache_pb2.Record) -> bool:
    return (record.created_at.seconds, record.created_at.nanos) > (than.created_at.seconds, than.created_at.nanos)


def _move_ref(manifest: litcache_pb2.Manifest, ref: str, key: str) -> bool:
    record = manifest.records[key]
    kind = record.WhichOneof('construction')
    rank = _RANKINGS.get(kind or '')
    if rank is None:
        raise RecordRefusedError(f'a {kind} record has no ranking, so no ref can name it')
    named = manifest.refs.get(ref)
    if named == key:
        return False
    if named is not None:
        current = manifest.records.get(named)
        if current is None or current.WhichOneof('construction') != kind:
            raise RecordRefusedError(
                f'manifest {manifest.doc_id}: the {ref} ref names {named!r}, which is not a {kind} it holds'
            )
        if rank(manifest, named, current) <= rank(manifest, key, record):
            return False
    manifest.refs[ref] = key
    return True


def put_record(manifest: litcache_pb2.Manifest, key: str, record: litcache_pb2.Record, ref: str | None) -> bool:
    """Hold `record` under `key`, and move `ref` to it when it outranks the record named; return whether either changed.

    Bytes produced again keep their key, and the record of the run that finished most recently is kept: `record`
    replaces a held one only when its `created_at` is later. `ref` moves only to `key`, never to another record, so a
    writer whose build ranks differently cannot move a ref it is not adding to. When the replaced record is the one
    `ref` names and its new construction ranks lower, the ref stays: the bytes it serves are the same.

    Args:
        manifest: The manifest to change in place.
        key: The sha256 of the record's bytes.
        record: How those bytes were made.
        ref: The ref the record competes for (`MARKDOWN_REF` for the paper's text), or None for a record no ref names.

    Raises:
        RecordRefusedError: If `key` is held as a record of another kind, or `ref` is given for a kind with no
            ranking, or names a record of another kind or one the manifest does not hold.
    """
    held = manifest.records.get(key)
    if held is not None and held.WhichOneof('construction') != record.WhichOneof('construction'):
        raise RecordRefusedError(
            f'manifest {manifest.doc_id}: {key} is held as a {held.WhichOneof("construction")}, '
            f'not a {record.WhichOneof("construction")}'
        )
    changed = held is None or _newer(record, held)
    if changed:
        manifest.records[key].CopyFrom(record)
    if ref is not None:
        changed = _move_ref(manifest, ref, key) or changed
    return changed


def is_rendering(record: litcache_pb2.Record) -> bool:
    """Whether `record` is a rendering: markdown a reader quotes from."""
    return record.WhichOneof('construction') == 'rendering'


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


def named(manifest: litcache_pb2.Manifest, ref: str, kind: str) -> tuple[str, litcache_pb2.Record] | None:
    """The record `ref` names, as `(key, record)`, or None when the ref is unset.

    Args:
        manifest: The paper's manifest.
        ref: The ref to follow.
        kind: The construction arm the named record must have.

    Raises:
        ValueError: If the ref names a record the manifest does not hold, or one of another kind: a corrupt manifest.
    """
    key = manifest.refs.get(ref)
    if key is None:
        return None
    record = manifest.records.get(key)
    if record is None or record.WhichOneof('construction') != kind:
        raise ValueError(f'manifest {manifest.doc_id}: the {ref} ref names {key!r}, which is not a {kind} it holds')
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
