"""The rules `SourcedMetadata`, `MetadataSource` and `Metadata` declare, and the code check that enforces them."""

from __future__ import annotations

import protovalidate
import pytest
from pubmed_proto import pubmed_pb2

from themis.litcache import sourced_metadata
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
        _sourced(litcache_pb2.MetadataSource(pubmed=_pubmed()), mesh_headings=(_heading('D001943', 'Q12'),)),
        'string.pattern',
        id='short-qualifier-ui',
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


# Valid to protovalidate, refused by the code check on a rule of its own: a JSON record that is not a JSON object.
_ONLY_THE_CODE_CHECK_REFUSES = [
    _sourced(litcache_pb2.MetadataSource(crossref_json='[]'), title='T'),
    _sourced(litcache_pb2.MetadataSource(openalex_json='"W1"'), title='T'),
]


@pytest.mark.parametrize('sourced', _ONLY_THE_CODE_CHECK_REFUSES)
def test_a_json_record_that_is_not_an_object_is_refused_by_the_code_check_alone(
    sourced: litcache_pb2.SourcedMetadata,
) -> None:
    protovalidate.validate(sourced)  # the proto can state only a minimum length
    with pytest.raises(ValueError, match='not a JSON object'):
        sourced_metadata.check(sourced)


# Every way `sourced_metadata.check` refuses a record.
_REFUSALS = (
    r"no metadata|no source record|no index record set|both PubMed's and Crossref's|neither of its kinds"
    r'|not a JSON object|an empty name|not a bare ORCID iD|not four digits|no descriptor|not a MeSH unique identifier'
    r'|not a [DQ] term|an empty type'
)


@pytest.mark.parametrize('sourced', [*_VALID, *(param.values[0] for param in _INVALID)])
def test_the_code_check_agrees_with_the_declared_constraints(sourced: litcache_pb2.SourcedMetadata) -> None:
    # The proto declares the constraints; `sourced_metadata.check` enforces them at the boundary in code. Both must
    # settle every record the same way, or the declaration and the enforcement have drifted.
    try:
        protovalidate.validate(sourced)
    except protovalidate.ValidationError:
        with pytest.raises(ValueError, match=_REFUSALS):
            sourced_metadata.check(sourced)
    else:
        sourced_metadata.check(sourced)  # does not raise


def test_every_source_record_field_is_one_index() -> None:
    # The code check counts indexes through INDEX_FIELDS, so a field missing from it would escape the count.
    declared = sorted(f.name for f in litcache_pb2.MetadataSource.DESCRIPTOR.fields)
    assert sorted(sourced_metadata.INDEX_FIELDS.values()) == declared
