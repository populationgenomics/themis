"""The `metadata.pb` envelope: its bytes, its constraints, and the title derived from it.

`PaperMetadata` holds the record of the index that resolved the paper, whole
(docs/design/litcache-manifest.md, "The bibliographic record"): PubMed's typed, Crossref's or
OpenAlex's in its mirror and, beside it, as JSON text. Its constraints are enforced here in code on
the way to bytes and back: the ones the proto declares as protovalidate options — exactly one
index's record set, PubMed's in exactly one of its two kinds — and two it does not, that a JSON
field holds a JSON object and is set only beside its index's mirror. The title a consumer reads is
derived from the typed record (see `title`); nothing reads the JSON text yet.
"""

from __future__ import annotations

from google.protobuf import message
from pubmed_proto import xml_converter

from themis.litcache import mirror
from themis.litcache.models import litcache_pb2

# Each index's fields in the envelope; a Crossref or OpenAlex record is held in its mirror and as JSON text.
INDEX_FIELDS: dict[str, tuple[str, ...]] = {
    'pubmed': ('pubmed',),
    'crossref': ('crossref', 'crossref_json'),
    'openalex': ('openalex', 'openalex_json'),
}
# Each JSON field, and the mirror field the same response fills beside it.
_JSON_FIELDS = (('crossref_json', 'crossref'), ('openalex_json', 'openalex'))


def to_canonical_bytes(envelope: litcache_pb2.PaperMetadata) -> bytes:
    """Serialize an envelope to litcache's canonical `metadata.pb` bytes.

    Raises:
        ValueError: If the envelope violates its constraints (see `check`).
    """
    check(envelope)
    return envelope.SerializeToString()


def parse(data: bytes) -> litcache_pb2.PaperMetadata:
    """Parse `metadata.pb` bytes into an envelope that meets its constraints.

    Raises:
        ValueError: If the bytes do not decode as a `PaperMetadata`, or decode as one that violates
            its constraints — corruption, never a paper without metadata.
    """
    try:
        envelope = litcache_pb2.PaperMetadata.FromString(data)
    except message.DecodeError as e:
        raise ValueError('metadata is not a valid PaperMetadata envelope') from e
    check(envelope)
    return envelope


def check(envelope: litcache_pb2.PaperMetadata) -> None:
    """Raise unless the envelope meets its constraints (see the module docstring).

    Raises:
        ValueError: If no index's record is set or more than one index's is, PubMed's record is set
            in neither of its kinds, or a JSON record is set to text that is not a JSON object, or
            without its index's mirror.
    """
    indexes = [index for index, fields in INDEX_FIELDS.items() if any(envelope.HasField(f) for f in fields)]
    if not indexes:
        raise ValueError('metadata is a PaperMetadata envelope with no record set')
    if len(indexes) > 1:
        raise ValueError(f'metadata holds records of more than one index: {", ".join(indexes)}')
    if envelope.HasField('pubmed') and envelope.pubmed.WhichOneof('kind') is None:
        raise ValueError('metadata carries a PubmedRecord in neither of its kinds')
    for field, mirror_field in _JSON_FIELDS:
        if not envelope.HasField(field):
            continue
        try:
            mirror.parse_text(getattr(envelope, field))
        except ValueError as e:
            raise ValueError(f'metadata field {field} is not a JSON object: {e}') from e
        if not envelope.HasField(mirror_field):
            raise ValueError(f'metadata field {field} is set without its mirror {mirror_field}')


def title(envelope: litcache_pb2.PaperMetadata) -> str:
    """The title the envelope's record states, as plain text, or the empty string when it states none.

    PubMed's book record states its chapter's title, or the book's when the record is a whole book's.
    PubMed's converter writes inline markup as markdown; it is stripped here.

    Raises:
        ValueError: If the envelope violates its constraints (see `check`).
    """
    check(envelope)
    return _pubmed_title(envelope) or _crossref_title(envelope) or _openalex_title(envelope)


def _pubmed_title(envelope: litcache_pb2.PaperMetadata) -> str:
    if not envelope.HasField('pubmed'):
        return ''
    record = envelope.pubmed
    match record.WhichOneof('kind'):
        case 'article':
            title = record.article.medline_citation.article.article_title.value
        case 'book_article':
            document = record.book_article.book_document
            title = document.article_title.value or document.book.book_title.value
        case kind:
            raise ValueError(f'PubmedRecord kind {kind!r} has no title reader')
    return xml_converter.markdown_to_plain_text(title)


def _crossref_title(envelope: litcache_pb2.PaperMetadata) -> str:
    if not envelope.HasField('crossref'):
        return ''
    return next((stated for stated in envelope.crossref.title if stated), '')


def _openalex_title(envelope: litcache_pb2.PaperMetadata) -> str:
    if not envelope.HasField('openalex'):
        return ''
    return envelope.openalex.title
