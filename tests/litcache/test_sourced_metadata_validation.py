"""The rules `SourcedMetadata`, `MetadataSource` and `Metadata` declare, as protovalidate applies them."""

from __future__ import annotations

import protovalidate
import pytest
from pubmed_proto import pubmed_pb2

from themis.litcache.models import litcache_pb2


def _pubmed() -> litcache_pb2.PubmedRecord:
    return litcache_pb2.PubmedRecord(article=pubmed_pb2.PubmedArticle())


def _sourced(
    source: litcache_pb2.MetadataSource,
    *,
    title: str = '',
    authors: tuple[litcache_pb2.Metadata.Author, ...] = (),
    year: str = '',
    container_title: str = '',
    abstract: str = '',
    mesh_headings: tuple[litcache_pb2.Metadata.MeshHeading, ...] = (),
    publication_types: tuple[str, ...] = (),
) -> litcache_pb2.SourcedMetadata:
    metadata = litcache_pb2.Metadata(
        title=title,
        authors=authors,
        year=year,
        container_title=container_title,
        abstract=abstract,
        mesh_headings=mesh_headings,
        publication_types=publication_types,
    )
    return litcache_pb2.SourcedMetadata(metadata=metadata, source=source)


def _author(name: str = 'Jane Doe', orcid: str = '') -> litcache_pb2.Metadata.Author:
    return litcache_pb2.Metadata.Author(name=name, orcid=orcid)


def _term(ui: str, *, major_topic: bool = False) -> litcache_pb2.Metadata.MeshTerm:
    return litcache_pb2.Metadata.MeshTerm(ui=ui, major_topic=major_topic)


def _heading(descriptor: str = 'D001943', *qualifiers: str) -> litcache_pb2.Metadata.MeshHeading:
    return litcache_pb2.Metadata.MeshHeading(descriptor=_term(descriptor), qualifiers=[_term(q) for q in qualifiers])


_VALID = [
    _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), title='T', authors=(_author(),), year='2020'),
    _sourced(
        litcache_pb2.MetadataSource(pubmed=_pubmed()),
        authors=(_author(orcid='0000-0002-1825-0097'), _author('John Roe', orcid='0000-0002-1694-233X')),
        mesh_headings=(_heading('D001943', 'Q000235', 'Q000000981'), _heading('D000076942')),
        publication_types=('Case Reports', 'Preprint'),
    ),
    _sourced(
        litcache_pb2.MetadataSource(crossref_json='{"DOI": "10.1/x"}'), title='T', container_title='J', abstract='A'
    ),
    _sourced(litcache_pb2.MetadataSource(openalex_json='{"id": "W1"}'), title=''),  # a record stating no title
    _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed())),  # filled from a record that states nothing
    _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed(), openalex_json='{"id": "W1"}'), title='T'),
    _sourced(litcache_pb2.MetadataSource(crossref_json='{"DOI": "10.1/x"}', openalex_json='{"id": "W1"}'), title='T'),
]
_INVALID = [
    pytest.param(
        litcache_pb2.SourcedMetadata(source=litcache_pb2.MetadataSource(pubmed=_pubmed())), 'required', id='no-metadata'
    ),
    pytest.param(litcache_pb2.SourcedMetadata(metadata=litcache_pb2.Metadata(title='T')), 'required', id='no-source'),
    pytest.param(_sourced(litcache_pb2.MetadataSource(), title='T'), 'metadata_source.indexes', id='no-index'),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed(), crossref_json='{"DOI": "10.1/x"}'), title='T'),
        'metadata_source.indexes',
        id='pubmed-and-crossref',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(crossref_json=''), title='T'), 'string.min_len', id='empty-crossref-json'
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(openalex_json='x'), title='T'),
        'string.min_len',
        id='openalex-json-too-short',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), authors=(_author(), _author(''))),
        'string.min_len',
        id='empty-author-name',
    ),
    pytest.param(
        _sourced(
            litcache_pb2.MetadataSource(pubmed=_pubmed()),
            authors=(_author(orcid='https://orcid.org/0000-0002-1825-0097'),),
        ),
        'string.pattern',
        id='orcid-as-url',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), authors=(_author(orcid='0000-0002-1825-009x'),)),
        'string.pattern',
        id='orcid-lowercase-check-digit',
    ),
    pytest.param(
        _sourced(
            litcache_pb2.MetadataSource(pubmed=_pubmed()),
            mesh_headings=(litcache_pb2.Metadata.MeshHeading(qualifiers=[_term('Q000235')]),),
        ),
        'required',
        id='heading-without-descriptor',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), mesh_headings=(_heading('Q000235'),)),
        'mesh_heading.descriptor_kind',
        id='qualifier-as-descriptor',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), mesh_headings=(_heading('D001943', 'D006801'),)),
        'mesh_heading.qualifier_kind',
        id='descriptor-as-qualifier',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), mesh_headings=(_heading('D19'),)),
        'string.pattern',
        id='short-mesh-ui',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), mesh_headings=(_heading('D0019430'),)),
        'string.pattern',
        id='seven-digit-mesh-ui',
    ),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), publication_types=('Case Reports', '')),
        'string.min_len',
        id='empty-publication-type',
    ),
    pytest.param(_sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), year='20'), 'string.pattern', id='short-year'),
    pytest.param(
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), year='c. 2020'), 'string.pattern', id='prose-year'
    ),
]


@pytest.mark.parametrize('sourced', _VALID)
def test_sourced_metadata_meeting_its_rules_is_valid(sourced: litcache_pb2.SourcedMetadata) -> None:
    protovalidate.validate(sourced)  # does not raise


@pytest.mark.parametrize(('sourced', 'rule_id'), _INVALID)
def test_sourced_metadata_breaking_a_rule_is_invalid(sourced: litcache_pb2.SourcedMetadata, rule_id: str) -> None:
    with pytest.raises(protovalidate.ValidationError) as raised:
        protovalidate.validate(sourced)
    assert [violation.proto.rule_id for violation in raised.value.violations] == [rule_id]
