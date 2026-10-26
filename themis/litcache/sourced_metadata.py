"""`metadata.pb`: a paper's `SourcedMetadata`, its bytes and its constraints.

A `SourcedMetadata` holds the paper's `Metadata`, which is what a consumer reads, beside the metadata's source record,
the `MetadataSource` it was filled from (docs/design/litcache-manifest.md, "The bibliographic record"). A consumer takes
the `Metadata` alone (`read_metadata`), so it cannot reach into the source record by accident. The constraints are
enforced here in code on the way to bytes and back: the ones the proto declares as protovalidate options — both parts
set, at least one index's record in the source and never both PubMed's and Crossref's, PubMed's in exactly one of its
two kinds, no empty author name, an ORCID iD in its bare form, a four-digit year, a MeSH heading's descriptor set and a
D term and its qualifiers Q terms, each in NLM's form, and no empty publication type — and one it cannot state, that a
JSON record is the text of a JSON object.
"""

from __future__ import annotations

import re

from google.protobuf import message

from themis.litcache import upstream_record
from themis.litcache.models import litcache_pb2

# The source record's field for each index.
INDEX_FIELDS: dict[str, str] = {'pubmed': 'pubmed', 'crossref': 'crossref_json', 'openalex': 'openalex_json'}
# The form the proto's `year` pattern declares: four digits, or none.
_YEAR = re.compile(r'([0-9]{4})?')
# The forms the proto's `orcid` and MeSH `ui` patterns declare: a bare iD or none, and NLM's unique identifier.
_ORCID = re.compile(r'([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X])?')
_MESH_UI = re.compile(r'[DQ]([0-9]{6}|[0-9]{9})')
_JSON_FIELDS = ('crossref_json', 'openalex_json')


def to_canonical_bytes(sourced: litcache_pb2.SourcedMetadata) -> bytes:
    """Serialize a `SourcedMetadata` to litcache's canonical `metadata.pb` bytes.

    Raises:
        ValueError: If it violates its constraints (see `check`).
    """
    check(sourced)
    return sourced.SerializeToString()


def parse(data: bytes) -> litcache_pb2.SourcedMetadata:
    """Parse `metadata.pb` bytes into a `SourcedMetadata` that meets its constraints.

    Raises:
        ValueError: If the bytes do not decode as a `SourcedMetadata`, or decode as one that violates its
            constraints — corruption, never a paper without metadata.
    """
    sourced = _decode(data)
    check(sourced)
    return sourced


def parse_to_refill(data: bytes) -> litcache_pb2.SourcedMetadata:
    """Parse `metadata.pb` bytes for a refill of its metadata, checking the source record alone.

    The refill replaces the metadata whole, so metadata that breaks a rule tightened since it was written must not stop
    the refill that brings it up to that rule. The refilled record is checked whole when it is written back.

    Raises:
        ValueError: If the bytes do not decode as a `SourcedMetadata`, or its source record is not set or breaks its
            constraints (`check_source`).
    """
    sourced = _decode(data)
    if not sourced.HasField('source'):
        raise ValueError('metadata.pb has no source record')
    check_source(sourced.source)
    return sourced


def _decode(data: bytes) -> litcache_pb2.SourcedMetadata:
    try:
        return litcache_pb2.SourcedMetadata.FromString(data)
    except message.DecodeError as e:
        raise ValueError('metadata.pb is not a valid SourcedMetadata') from e


def read_metadata(data: bytes) -> litcache_pb2.Metadata:
    """The paper's `Metadata` from `metadata.pb` bytes: what a consumer reads, and all it is given.

    Raises:
        ValueError: As `parse`.
    """
    return parse(data).metadata


def check(sourced: litcache_pb2.SourcedMetadata) -> None:
    """Raise unless a `SourcedMetadata` meets its constraints (see the module docstring).

    Raises:
        ValueError: If its metadata or its source record is not set, or either breaks its constraints
            (`check_source`, `check_metadata`).
    """
    if not sourced.HasField('source'):
        raise ValueError('metadata.pb has no source record')
    if not sourced.HasField('metadata'):
        raise ValueError('metadata.pb has no metadata: it is not filled')
    check_source(sourced.source)
    check_metadata(sourced.metadata)


def check_source(source: litcache_pb2.MetadataSource) -> None:
    """Raise unless the metadata's source record meets its constraints.

    Raises:
        ValueError: If no index's record is set or both PubMed's and Crossref's are, PubMed's record is set in neither
            of its kinds, or a JSON record is set to text that is not a JSON object.
    """
    if not any(source.HasField(field) for field in INDEX_FIELDS.values()):
        raise ValueError('the source record has no index record set')
    if source.HasField('pubmed') and source.HasField('crossref_json'):
        raise ValueError("the source record holds both PubMed's and Crossref's record")
    if source.HasField('pubmed') and source.pubmed.WhichOneof('kind') is None:
        raise ValueError('the source record carries a PubmedRecord in neither of its kinds')
    for field in _JSON_FIELDS:
        if not source.HasField(field):
            continue
        try:
            upstream_record.parse_text(getattr(source, field))
        except ValueError as e:
            raise ValueError(f'the source record field {field} is not a JSON object: {e}') from e


def check_metadata(metadata: litcache_pb2.Metadata) -> None:
    """Raise unless the `Metadata` meets the rules the proto declares for it.

    Raises:
        ValueError: If an author's name is empty or their ORCID iD is not in its bare form, the year is not four digits,
            a MeSH heading has no descriptor or holds a term not in the form or kind its place needs, or a publication
            type is empty.
    """
    for author in metadata.authors:
        if not author.name:
            raise ValueError('metadata field authors holds an empty name')
        if not _ORCID.fullmatch(author.orcid):
            raise ValueError(f'metadata field authors holds {author.orcid!r}, not a bare ORCID iD')
    if not _YEAR.fullmatch(metadata.year):
        raise ValueError(f'metadata field year is {metadata.year!r}, not four digits')
    for heading in metadata.mesh_headings:
        if not heading.HasField('descriptor'):
            raise ValueError('metadata field mesh_headings holds a heading with no descriptor')
        _check_mesh_term(heading.descriptor, 'D', 'descriptor')
        for qualifier in heading.qualifiers:
            _check_mesh_term(qualifier, 'Q', 'qualifier')
    if any(not publication_type for publication_type in metadata.publication_types):
        raise ValueError('metadata field publication_types holds an empty type')


def _check_mesh_term(term: litcache_pb2.Metadata.MeshTerm, kind: str, place: str) -> None:
    if not _MESH_UI.fullmatch(term.ui):
        raise ValueError(f'metadata field mesh_headings holds {term.ui!r}, not a MeSH unique identifier')
    if not term.ui.startswith(kind):
        raise ValueError(f'metadata field mesh_headings holds {term.ui!r} as a {place}, not a {kind} term')
