"""The `metadata.pb` envelope: its bytes, its constraints, and the title derived from it.

`PaperMetadata` holds the record of the index that resolved the paper, whole
(docs/design/litcache-manifest.md, "The bibliographic record"). Its constraints — exactly one
index's record set, PubMed's in exactly one of its two kinds — are protovalidate options on the
proto, enforced here in code on the way to bytes and back, together with one the proto cannot
state: a JSON record is the text of a JSON object. The title a consumer reads is derived from that
record.
"""

from __future__ import annotations

import json

from google.protobuf import message

from themis.litcache.models import litcache_pb2

# Each index's fields in the envelope; a Crossref or OpenAlex record is held in its mirror and as JSON text.
INDEX_FIELDS: dict[str, tuple[str, ...]] = {
    'pubmed': ('pubmed',),
    'crossref': ('crossref', 'crossref_json'),
    'openalex': ('openalex', 'openalex_json'),
}
_JSON_FIELDS = ('crossref_json', 'openalex_json')


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
    """Raise unless the envelope meets its constraints.

    Raises:
        ValueError: If no index's record is set or more than one index's is, PubMed's record is set
            in neither of its kinds, or a JSON record is set to text that is not a JSON object.
    """
    indexes = [index for index, fields in INDEX_FIELDS.items() if any(envelope.HasField(f) for f in fields)]
    if not indexes:
        raise ValueError('metadata is a PaperMetadata envelope with no record set')
    if len(indexes) > 1:
        raise ValueError(f'metadata holds records of more than one index: {", ".join(indexes)}')
    if envelope.HasField('pubmed') and envelope.pubmed.WhichOneof('kind') is None:
        raise ValueError('metadata carries a PubmedRecord in neither of its kinds')
    for name in _JSON_FIELDS:
        if envelope.HasField(name):
            _check_json_object(name, getattr(envelope, name))


def _check_json_object(name: str, text: str) -> None:
    try:
        record = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f'metadata field {name} is not a JSON object: {e}') from e
    if not isinstance(record, dict):
        raise ValueError(f'metadata field {name} is not a JSON object: it holds a {type(record).__name__}')


def title(envelope: litcache_pb2.PaperMetadata) -> str:
    """The title the envelope's record states, or the empty string when it states none.

    PubMed's book record states its chapter's title, or the book's when the record is a whole book's.

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
            return record.article.medline_citation.article.article_title.value
        case 'book_article':
            document = record.book_article.book_document
            return document.article_title.value or document.book.book_title.value
        case kind:
            raise ValueError(f'PubmedRecord kind {kind!r} has no title reader')


def _crossref_title(envelope: litcache_pb2.PaperMetadata) -> str:
    if not envelope.HasField('crossref'):
        return ''
    return next((stated for stated in envelope.crossref.title if stated), '')


def _openalex_title(envelope: litcache_pb2.PaperMetadata) -> str:
    if not envelope.HasField('openalex'):
        return ''
    return envelope.openalex.title
