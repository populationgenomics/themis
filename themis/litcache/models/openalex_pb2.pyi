from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Work(_message.Message):
    __slots__ = ("abstract_inverted_index", "apc_list", "apc_paid", "authorships", "awards", "best_oa_location", "biblio", "citation_normalized_percentile", "cited_by_count", "cited_by_percentile_year", "concepts", "content_url", "content_urls", "corresponding_author_ids", "corresponding_institution_ids", "countries_distinct_count", "counts_by_year", "created_date", "display_name", "doi", "funders", "fwci", "has_content", "has_fulltext", "id", "ids", "indexed_in", "institutions", "institutions_distinct_count", "is_paratext", "is_retracted", "is_xpac", "keywords", "language", "locations", "locations_count", "mesh", "open_access", "primary_location", "primary_topic", "publication_date", "publication_year", "referenced_works", "referenced_works_count", "related_works", "relevance_score", "sustainable_development_goals", "title", "topics", "type", "updated_date")
    class AbstractInvertedIndexEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: Positions
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[Positions, _Mapping]] = ...) -> None: ...
    ABSTRACT_INVERTED_INDEX_FIELD_NUMBER: _ClassVar[int]
    APC_LIST_FIELD_NUMBER: _ClassVar[int]
    APC_PAID_FIELD_NUMBER: _ClassVar[int]
    AUTHORSHIPS_FIELD_NUMBER: _ClassVar[int]
    AWARDS_FIELD_NUMBER: _ClassVar[int]
    BEST_OA_LOCATION_FIELD_NUMBER: _ClassVar[int]
    BIBLIO_FIELD_NUMBER: _ClassVar[int]
    CITATION_NORMALIZED_PERCENTILE_FIELD_NUMBER: _ClassVar[int]
    CITED_BY_COUNT_FIELD_NUMBER: _ClassVar[int]
    CITED_BY_PERCENTILE_YEAR_FIELD_NUMBER: _ClassVar[int]
    CONCEPTS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_URL_FIELD_NUMBER: _ClassVar[int]
    CONTENT_URLS_FIELD_NUMBER: _ClassVar[int]
    CORRESPONDING_AUTHOR_IDS_FIELD_NUMBER: _ClassVar[int]
    CORRESPONDING_INSTITUTION_IDS_FIELD_NUMBER: _ClassVar[int]
    COUNTRIES_DISTINCT_COUNT_FIELD_NUMBER: _ClassVar[int]
    COUNTS_BY_YEAR_FIELD_NUMBER: _ClassVar[int]
    CREATED_DATE_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    FUNDERS_FIELD_NUMBER: _ClassVar[int]
    FWCI_FIELD_NUMBER: _ClassVar[int]
    HAS_CONTENT_FIELD_NUMBER: _ClassVar[int]
    HAS_FULLTEXT_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    IDS_FIELD_NUMBER: _ClassVar[int]
    INDEXED_IN_FIELD_NUMBER: _ClassVar[int]
    INSTITUTIONS_FIELD_NUMBER: _ClassVar[int]
    INSTITUTIONS_DISTINCT_COUNT_FIELD_NUMBER: _ClassVar[int]
    IS_PARATEXT_FIELD_NUMBER: _ClassVar[int]
    IS_RETRACTED_FIELD_NUMBER: _ClassVar[int]
    IS_XPAC_FIELD_NUMBER: _ClassVar[int]
    KEYWORDS_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    LOCATIONS_FIELD_NUMBER: _ClassVar[int]
    LOCATIONS_COUNT_FIELD_NUMBER: _ClassVar[int]
    MESH_FIELD_NUMBER: _ClassVar[int]
    OPEN_ACCESS_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_LOCATION_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_TOPIC_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_DATE_FIELD_NUMBER: _ClassVar[int]
    PUBLICATION_YEAR_FIELD_NUMBER: _ClassVar[int]
    REFERENCED_WORKS_FIELD_NUMBER: _ClassVar[int]
    REFERENCED_WORKS_COUNT_FIELD_NUMBER: _ClassVar[int]
    RELATED_WORKS_FIELD_NUMBER: _ClassVar[int]
    RELEVANCE_SCORE_FIELD_NUMBER: _ClassVar[int]
    SUSTAINABLE_DEVELOPMENT_GOALS_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    TOPICS_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_DATE_FIELD_NUMBER: _ClassVar[int]
    abstract_inverted_index: _containers.MessageMap[str, Positions]
    apc_list: Apc
    apc_paid: Apc
    authorships: _containers.RepeatedCompositeFieldContainer[Authorship]
    awards: _containers.RepeatedCompositeFieldContainer[Award]
    best_oa_location: Location
    biblio: Biblio
    citation_normalized_percentile: CitationNormalizedPercentile
    cited_by_count: int
    cited_by_percentile_year: CitedByPercentileYear
    concepts: _containers.RepeatedCompositeFieldContainer[Concept]
    content_url: str
    content_urls: ContentUrls
    corresponding_author_ids: _containers.RepeatedScalarFieldContainer[str]
    corresponding_institution_ids: _containers.RepeatedScalarFieldContainer[str]
    countries_distinct_count: int
    counts_by_year: _containers.RepeatedCompositeFieldContainer[CountsByYear]
    created_date: str
    display_name: str
    doi: str
    funders: _containers.RepeatedCompositeFieldContainer[DehydratedFunder]
    fwci: float
    has_content: HasContent
    has_fulltext: bool
    id: str
    ids: Ids
    indexed_in: _containers.RepeatedScalarFieldContainer[str]
    institutions: _containers.RepeatedCompositeFieldContainer[DehydratedInstitution]
    institutions_distinct_count: int
    is_paratext: bool
    is_retracted: bool
    is_xpac: bool
    keywords: _containers.RepeatedCompositeFieldContainer[Keyword]
    language: str
    locations: _containers.RepeatedCompositeFieldContainer[Location]
    locations_count: int
    mesh: _containers.RepeatedCompositeFieldContainer[Mesh]
    open_access: OpenAccess
    primary_location: Location
    primary_topic: Topic
    publication_date: str
    publication_year: int
    referenced_works: _containers.RepeatedScalarFieldContainer[str]
    referenced_works_count: int
    related_works: _containers.RepeatedScalarFieldContainer[str]
    relevance_score: float
    sustainable_development_goals: _containers.RepeatedCompositeFieldContainer[SustainableDevelopmentGoal]
    title: str
    topics: _containers.RepeatedCompositeFieldContainer[Topic]
    type: str
    updated_date: str
    def __init__(self, abstract_inverted_index: _Optional[_Mapping[str, Positions]] = ..., apc_list: _Optional[_Union[Apc, _Mapping]] = ..., apc_paid: _Optional[_Union[Apc, _Mapping]] = ..., authorships: _Optional[_Iterable[_Union[Authorship, _Mapping]]] = ..., awards: _Optional[_Iterable[_Union[Award, _Mapping]]] = ..., best_oa_location: _Optional[_Union[Location, _Mapping]] = ..., biblio: _Optional[_Union[Biblio, _Mapping]] = ..., citation_normalized_percentile: _Optional[_Union[CitationNormalizedPercentile, _Mapping]] = ..., cited_by_count: _Optional[int] = ..., cited_by_percentile_year: _Optional[_Union[CitedByPercentileYear, _Mapping]] = ..., concepts: _Optional[_Iterable[_Union[Concept, _Mapping]]] = ..., content_url: _Optional[str] = ..., content_urls: _Optional[_Union[ContentUrls, _Mapping]] = ..., corresponding_author_ids: _Optional[_Iterable[str]] = ..., corresponding_institution_ids: _Optional[_Iterable[str]] = ..., countries_distinct_count: _Optional[int] = ..., counts_by_year: _Optional[_Iterable[_Union[CountsByYear, _Mapping]]] = ..., created_date: _Optional[str] = ..., display_name: _Optional[str] = ..., doi: _Optional[str] = ..., funders: _Optional[_Iterable[_Union[DehydratedFunder, _Mapping]]] = ..., fwci: _Optional[float] = ..., has_content: _Optional[_Union[HasContent, _Mapping]] = ..., has_fulltext: _Optional[bool] = ..., id: _Optional[str] = ..., ids: _Optional[_Union[Ids, _Mapping]] = ..., indexed_in: _Optional[_Iterable[str]] = ..., institutions: _Optional[_Iterable[_Union[DehydratedInstitution, _Mapping]]] = ..., institutions_distinct_count: _Optional[int] = ..., is_paratext: _Optional[bool] = ..., is_retracted: _Optional[bool] = ..., is_xpac: _Optional[bool] = ..., keywords: _Optional[_Iterable[_Union[Keyword, _Mapping]]] = ..., language: _Optional[str] = ..., locations: _Optional[_Iterable[_Union[Location, _Mapping]]] = ..., locations_count: _Optional[int] = ..., mesh: _Optional[_Iterable[_Union[Mesh, _Mapping]]] = ..., open_access: _Optional[_Union[OpenAccess, _Mapping]] = ..., primary_location: _Optional[_Union[Location, _Mapping]] = ..., primary_topic: _Optional[_Union[Topic, _Mapping]] = ..., publication_date: _Optional[str] = ..., publication_year: _Optional[int] = ..., referenced_works: _Optional[_Iterable[str]] = ..., referenced_works_count: _Optional[int] = ..., related_works: _Optional[_Iterable[str]] = ..., relevance_score: _Optional[float] = ..., sustainable_development_goals: _Optional[_Iterable[_Union[SustainableDevelopmentGoal, _Mapping]]] = ..., title: _Optional[str] = ..., topics: _Optional[_Iterable[_Union[Topic, _Mapping]]] = ..., type: _Optional[str] = ..., updated_date: _Optional[str] = ...) -> None: ...

class Positions(_message.Message):
    __slots__ = ("positions",)
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    positions: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, positions: _Optional[_Iterable[int]] = ...) -> None: ...

class Apc(_message.Message):
    __slots__ = ("currency", "value", "value_usd")
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    VALUE_USD_FIELD_NUMBER: _ClassVar[int]
    currency: str
    value: int
    value_usd: int
    def __init__(self, currency: _Optional[str] = ..., value: _Optional[int] = ..., value_usd: _Optional[int] = ...) -> None: ...

class Authorship(_message.Message):
    __slots__ = ("affiliations", "author", "author_position", "countries", "institutions", "is_corresponding", "raw_affiliation_strings", "raw_author_name", "raw_orcid")
    AFFILIATIONS_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_POSITION_FIELD_NUMBER: _ClassVar[int]
    COUNTRIES_FIELD_NUMBER: _ClassVar[int]
    INSTITUTIONS_FIELD_NUMBER: _ClassVar[int]
    IS_CORRESPONDING_FIELD_NUMBER: _ClassVar[int]
    RAW_AFFILIATION_STRINGS_FIELD_NUMBER: _ClassVar[int]
    RAW_AUTHOR_NAME_FIELD_NUMBER: _ClassVar[int]
    RAW_ORCID_FIELD_NUMBER: _ClassVar[int]
    affiliations: _containers.RepeatedCompositeFieldContainer[Affiliation]
    author: DehydratedAuthor
    author_position: str
    countries: _containers.RepeatedScalarFieldContainer[str]
    institutions: _containers.RepeatedCompositeFieldContainer[DehydratedInstitution]
    is_corresponding: bool
    raw_affiliation_strings: _containers.RepeatedScalarFieldContainer[str]
    raw_author_name: str
    raw_orcid: str
    def __init__(self, affiliations: _Optional[_Iterable[_Union[Affiliation, _Mapping]]] = ..., author: _Optional[_Union[DehydratedAuthor, _Mapping]] = ..., author_position: _Optional[str] = ..., countries: _Optional[_Iterable[str]] = ..., institutions: _Optional[_Iterable[_Union[DehydratedInstitution, _Mapping]]] = ..., is_corresponding: _Optional[bool] = ..., raw_affiliation_strings: _Optional[_Iterable[str]] = ..., raw_author_name: _Optional[str] = ..., raw_orcid: _Optional[str] = ...) -> None: ...

class Affiliation(_message.Message):
    __slots__ = ("institution_ids", "raw_affiliation_string")
    INSTITUTION_IDS_FIELD_NUMBER: _ClassVar[int]
    RAW_AFFILIATION_STRING_FIELD_NUMBER: _ClassVar[int]
    institution_ids: _containers.RepeatedScalarFieldContainer[str]
    raw_affiliation_string: str
    def __init__(self, institution_ids: _Optional[_Iterable[str]] = ..., raw_affiliation_string: _Optional[str] = ...) -> None: ...

class DehydratedAuthor(_message.Message):
    __slots__ = ("display_name", "id", "orcid")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    ORCID_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    id: str
    orcid: str
    def __init__(self, display_name: _Optional[str] = ..., id: _Optional[str] = ..., orcid: _Optional[str] = ...) -> None: ...

class DehydratedInstitution(_message.Message):
    __slots__ = ("country_code", "display_name", "id", "lineage", "ror", "type")
    COUNTRY_CODE_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    LINEAGE_FIELD_NUMBER: _ClassVar[int]
    ROR_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    country_code: str
    display_name: str
    id: str
    lineage: _containers.RepeatedScalarFieldContainer[str]
    ror: str
    type: str
    def __init__(self, country_code: _Optional[str] = ..., display_name: _Optional[str] = ..., id: _Optional[str] = ..., lineage: _Optional[_Iterable[str]] = ..., ror: _Optional[str] = ..., type: _Optional[str] = ...) -> None: ...

class Award(_message.Message):
    __slots__ = ("display_name", "doi", "funder_award_id", "funder_display_name", "funder_id", "id")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    FUNDER_AWARD_ID_FIELD_NUMBER: _ClassVar[int]
    FUNDER_DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    FUNDER_ID_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    doi: str
    funder_award_id: str
    funder_display_name: str
    funder_id: str
    id: str
    def __init__(self, display_name: _Optional[str] = ..., doi: _Optional[str] = ..., funder_award_id: _Optional[str] = ..., funder_display_name: _Optional[str] = ..., funder_id: _Optional[str] = ..., id: _Optional[str] = ...) -> None: ...

class Biblio(_message.Message):
    __slots__ = ("first_page", "issue", "last_page", "volume")
    FIRST_PAGE_FIELD_NUMBER: _ClassVar[int]
    ISSUE_FIELD_NUMBER: _ClassVar[int]
    LAST_PAGE_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    first_page: str
    issue: str
    last_page: str
    volume: str
    def __init__(self, first_page: _Optional[str] = ..., issue: _Optional[str] = ..., last_page: _Optional[str] = ..., volume: _Optional[str] = ...) -> None: ...

class CitationNormalizedPercentile(_message.Message):
    __slots__ = ("is_in_top_10_percent", "is_in_top_1_percent", "value")
    IS_IN_TOP_10_PERCENT_FIELD_NUMBER: _ClassVar[int]
    IS_IN_TOP_1_PERCENT_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    is_in_top_10_percent: bool
    is_in_top_1_percent: bool
    value: float
    def __init__(self, is_in_top_10_percent: _Optional[bool] = ..., is_in_top_1_percent: _Optional[bool] = ..., value: _Optional[float] = ...) -> None: ...

class CitedByPercentileYear(_message.Message):
    __slots__ = ("max", "min")
    MAX_FIELD_NUMBER: _ClassVar[int]
    MIN_FIELD_NUMBER: _ClassVar[int]
    max: int
    min: int
    def __init__(self, max: _Optional[int] = ..., min: _Optional[int] = ...) -> None: ...

class Concept(_message.Message):
    __slots__ = ("display_name", "id", "level", "score", "wikidata")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    WIKIDATA_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    id: str
    level: int
    score: float
    wikidata: str
    def __init__(self, display_name: _Optional[str] = ..., id: _Optional[str] = ..., level: _Optional[int] = ..., score: _Optional[float] = ..., wikidata: _Optional[str] = ...) -> None: ...

class ContentUrls(_message.Message):
    __slots__ = ("grobid_xml", "pdf")
    GROBID_XML_FIELD_NUMBER: _ClassVar[int]
    PDF_FIELD_NUMBER: _ClassVar[int]
    grobid_xml: str
    pdf: str
    def __init__(self, grobid_xml: _Optional[str] = ..., pdf: _Optional[str] = ...) -> None: ...

class CountsByYear(_message.Message):
    __slots__ = ("cited_by_count", "year")
    CITED_BY_COUNT_FIELD_NUMBER: _ClassVar[int]
    YEAR_FIELD_NUMBER: _ClassVar[int]
    cited_by_count: int
    year: int
    def __init__(self, cited_by_count: _Optional[int] = ..., year: _Optional[int] = ...) -> None: ...

class DehydratedFunder(_message.Message):
    __slots__ = ("display_name", "id", "ror")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    ROR_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    id: str
    ror: str
    def __init__(self, display_name: _Optional[str] = ..., id: _Optional[str] = ..., ror: _Optional[str] = ...) -> None: ...

class HasContent(_message.Message):
    __slots__ = ("grobid_xml", "pdf")
    GROBID_XML_FIELD_NUMBER: _ClassVar[int]
    PDF_FIELD_NUMBER: _ClassVar[int]
    grobid_xml: bool
    pdf: bool
    def __init__(self, grobid_xml: _Optional[bool] = ..., pdf: _Optional[bool] = ...) -> None: ...

class Ids(_message.Message):
    __slots__ = ("doi", "mag", "openalex", "pmcid", "pmid")
    DOI_FIELD_NUMBER: _ClassVar[int]
    MAG_FIELD_NUMBER: _ClassVar[int]
    OPENALEX_FIELD_NUMBER: _ClassVar[int]
    PMCID_FIELD_NUMBER: _ClassVar[int]
    PMID_FIELD_NUMBER: _ClassVar[int]
    doi: str
    mag: str
    openalex: str
    pmcid: str
    pmid: str
    def __init__(self, doi: _Optional[str] = ..., mag: _Optional[str] = ..., openalex: _Optional[str] = ..., pmcid: _Optional[str] = ..., pmid: _Optional[str] = ...) -> None: ...

class Keyword(_message.Message):
    __slots__ = ("display_name", "id", "score")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    id: str
    score: float
    def __init__(self, display_name: _Optional[str] = ..., id: _Optional[str] = ..., score: _Optional[float] = ...) -> None: ...

class Location(_message.Message):
    __slots__ = ("id", "is_accepted", "is_oa", "is_published", "landing_page_url", "license", "license_id", "pdf_url", "raw_source_name", "raw_type", "source", "version")
    ID_FIELD_NUMBER: _ClassVar[int]
    IS_ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    IS_OA_FIELD_NUMBER: _ClassVar[int]
    IS_PUBLISHED_FIELD_NUMBER: _ClassVar[int]
    LANDING_PAGE_URL_FIELD_NUMBER: _ClassVar[int]
    LICENSE_FIELD_NUMBER: _ClassVar[int]
    LICENSE_ID_FIELD_NUMBER: _ClassVar[int]
    PDF_URL_FIELD_NUMBER: _ClassVar[int]
    RAW_SOURCE_NAME_FIELD_NUMBER: _ClassVar[int]
    RAW_TYPE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    id: str
    is_accepted: bool
    is_oa: bool
    is_published: bool
    landing_page_url: str
    license: str
    license_id: str
    pdf_url: str
    raw_source_name: str
    raw_type: str
    source: DehydratedSource
    version: str
    def __init__(self, id: _Optional[str] = ..., is_accepted: _Optional[bool] = ..., is_oa: _Optional[bool] = ..., is_published: _Optional[bool] = ..., landing_page_url: _Optional[str] = ..., license: _Optional[str] = ..., license_id: _Optional[str] = ..., pdf_url: _Optional[str] = ..., raw_source_name: _Optional[str] = ..., raw_type: _Optional[str] = ..., source: _Optional[_Union[DehydratedSource, _Mapping]] = ..., version: _Optional[str] = ...) -> None: ...

class DehydratedSource(_message.Message):
    __slots__ = ("display_name", "host_organization", "host_organization_lineage", "host_organization_lineage_names", "host_organization_name", "id", "is_core", "is_in_doaj", "is_oa", "issn", "issn_l", "type")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    HOST_ORGANIZATION_FIELD_NUMBER: _ClassVar[int]
    HOST_ORGANIZATION_LINEAGE_FIELD_NUMBER: _ClassVar[int]
    HOST_ORGANIZATION_LINEAGE_NAMES_FIELD_NUMBER: _ClassVar[int]
    HOST_ORGANIZATION_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    IS_CORE_FIELD_NUMBER: _ClassVar[int]
    IS_IN_DOAJ_FIELD_NUMBER: _ClassVar[int]
    IS_OA_FIELD_NUMBER: _ClassVar[int]
    ISSN_FIELD_NUMBER: _ClassVar[int]
    ISSN_L_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    host_organization: str
    host_organization_lineage: _containers.RepeatedScalarFieldContainer[str]
    host_organization_lineage_names: _containers.RepeatedScalarFieldContainer[str]
    host_organization_name: str
    id: str
    is_core: bool
    is_in_doaj: bool
    is_oa: bool
    issn: _containers.RepeatedScalarFieldContainer[str]
    issn_l: str
    type: str
    def __init__(self, display_name: _Optional[str] = ..., host_organization: _Optional[str] = ..., host_organization_lineage: _Optional[_Iterable[str]] = ..., host_organization_lineage_names: _Optional[_Iterable[str]] = ..., host_organization_name: _Optional[str] = ..., id: _Optional[str] = ..., is_core: _Optional[bool] = ..., is_in_doaj: _Optional[bool] = ..., is_oa: _Optional[bool] = ..., issn: _Optional[_Iterable[str]] = ..., issn_l: _Optional[str] = ..., type: _Optional[str] = ...) -> None: ...

class Mesh(_message.Message):
    __slots__ = ("descriptor_name", "descriptor_ui", "is_major_topic", "qualifier_name", "qualifier_ui")
    DESCRIPTOR_NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTOR_UI_FIELD_NUMBER: _ClassVar[int]
    IS_MAJOR_TOPIC_FIELD_NUMBER: _ClassVar[int]
    QUALIFIER_NAME_FIELD_NUMBER: _ClassVar[int]
    QUALIFIER_UI_FIELD_NUMBER: _ClassVar[int]
    descriptor_name: str
    descriptor_ui: str
    is_major_topic: bool
    qualifier_name: str
    qualifier_ui: str
    def __init__(self, descriptor_name: _Optional[str] = ..., descriptor_ui: _Optional[str] = ..., is_major_topic: _Optional[bool] = ..., qualifier_name: _Optional[str] = ..., qualifier_ui: _Optional[str] = ...) -> None: ...

class OpenAccess(_message.Message):
    __slots__ = ("any_repository_has_fulltext", "is_oa", "oa_status", "oa_url")
    ANY_REPOSITORY_HAS_FULLTEXT_FIELD_NUMBER: _ClassVar[int]
    IS_OA_FIELD_NUMBER: _ClassVar[int]
    OA_STATUS_FIELD_NUMBER: _ClassVar[int]
    OA_URL_FIELD_NUMBER: _ClassVar[int]
    any_repository_has_fulltext: bool
    is_oa: bool
    oa_status: str
    oa_url: str
    def __init__(self, any_repository_has_fulltext: _Optional[bool] = ..., is_oa: _Optional[bool] = ..., oa_status: _Optional[str] = ..., oa_url: _Optional[str] = ...) -> None: ...

class SustainableDevelopmentGoal(_message.Message):
    __slots__ = ("display_name", "id", "score")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    id: str
    score: float
    def __init__(self, display_name: _Optional[str] = ..., id: _Optional[str] = ..., score: _Optional[float] = ...) -> None: ...

class Topic(_message.Message):
    __slots__ = ("display_name", "domain", "field", "id", "score", "subfield")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_FIELD_NUMBER: _ClassVar[int]
    FIELD_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    SUBFIELD_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    domain: TopicLevel
    field: TopicLevel
    id: str
    score: float
    subfield: TopicLevel
    def __init__(self, display_name: _Optional[str] = ..., domain: _Optional[_Union[TopicLevel, _Mapping]] = ..., field: _Optional[_Union[TopicLevel, _Mapping]] = ..., id: _Optional[str] = ..., score: _Optional[float] = ..., subfield: _Optional[_Union[TopicLevel, _Mapping]] = ...) -> None: ...

class TopicLevel(_message.Message):
    __slots__ = ("display_name", "id")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    id: str
    def __init__(self, display_name: _Optional[str] = ..., id: _Optional[str] = ...) -> None: ...
