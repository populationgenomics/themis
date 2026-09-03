"""The `metadata.pb` envelope: its bytes, its constraints, and the summary derived from it.

`PaperMetadata` holds each resolving index's record whole (docs/design/litcache-manifest.md, "The
bibliographic record"). Its constraints — at least one index's record set, PubMed's in exactly
one of its two kinds — are protovalidate options on the proto, enforced here in code on the way to
bytes and back. The summary a consumer reads in one shape — the title, today — is derived from
whichever records are present, PubMed's first.
"""

from __future__ import annotations

from google.protobuf import message

from themis.litcache.models import litcache_pb2

_INDEX_FIELDS = tuple(field.name for field in litcache_pb2.PaperMetadata.DESCRIPTOR.fields)


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
    """Raise unless the envelope meets the constraints its proto declares.

    Raises:
        ValueError: If no index's record is set, or PubMed's record is set in neither of its kinds.
    """
    if not any(envelope.HasField(name) for name in _INDEX_FIELDS):
        raise ValueError('metadata is a PaperMetadata envelope with no record set')
    if envelope.HasField('pubmed') and envelope.pubmed.WhichOneof('kind') is None:
        raise ValueError('metadata carries a PubmedRecord in neither of its kinds')


def title(envelope: litcache_pb2.PaperMetadata) -> str:
    """The title the envelope's records state, or the empty string when none states one.

    Records are read in order of precedence — PubMed's, then Crossref's, then OpenAlex's — and the
    first that states a title wins. PubMed's book record states its chapter's title, or the book's
    when the record is a whole book's.

    Raises:
        ValueError: If the envelope violates its constraints (see `check`).
    """
    check(envelope)
    for stated in (_pubmed_title(envelope), _crossref_title(envelope), _openalex_title(envelope)):
        if stated:
            return stated
    return ''


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
