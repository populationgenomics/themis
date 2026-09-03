from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Work(_message.Message):
    __slots__ = ("abstract", "accepted", "aliases", "alternative_id", "approved", "archive", "article_number", "assertion", "author", "award", "award_start", "chair", "clinical_trial_number", "component_number", "container_title", "content_created", "content_domain", "content_updated", "contributor", "created", "degree", "deposited", "description", "doi", "edition_number", "editor", "event", "free_to_read", "funder", "group_title", "indexed", "institution", "is_referenced_by_count", "isbn", "isbn_type", "issn", "issn_type", "issue", "issue_title", "issued", "journal_issue", "language", "license", "link", "member", "original_title", "page", "part_number", "posted", "prefix", "proceedings_subject", "project", "published", "published_online", "published_other", "published_print", "publisher", "publisher_location", "reference", "reference_count", "references_count", "relation", "resource", "review", "score", "short_container_title", "short_title", "source", "special_numbering", "standards_body", "status", "subject", "subtitle", "subtype", "title", "translator", "type", "update_policy", "update_to", "updated_by", "url", "version", "volume")
    class RelationEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: RelationList
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[RelationList, _Mapping]] = ...) -> None: ...
    ABSTRACT_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    ALIASES_FIELD_NUMBER: _ClassVar[int]
    ALTERNATIVE_ID_FIELD_NUMBER: _ClassVar[int]
    APPROVED_FIELD_NUMBER: _ClassVar[int]
    ARCHIVE_FIELD_NUMBER: _ClassVar[int]
    ARTICLE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    ASSERTION_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    AWARD_FIELD_NUMBER: _ClassVar[int]
    AWARD_START_FIELD_NUMBER: _ClassVar[int]
    CHAIR_FIELD_NUMBER: _ClassVar[int]
    CLINICAL_TRIAL_NUMBER_FIELD_NUMBER: _ClassVar[int]
    COMPONENT_NUMBER_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_TITLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_CREATED_FIELD_NUMBER: _ClassVar[int]
    CONTENT_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    CONTENT_UPDATED_FIELD_NUMBER: _ClassVar[int]
    CONTRIBUTOR_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    DEGREE_FIELD_NUMBER: _ClassVar[int]
    DEPOSITED_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    EDITION_NUMBER_FIELD_NUMBER: _ClassVar[int]
    EDITOR_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    FREE_TO_READ_FIELD_NUMBER: _ClassVar[int]
    FUNDER_FIELD_NUMBER: _ClassVar[int]
    GROUP_TITLE_FIELD_NUMBER: _ClassVar[int]
    INDEXED_FIELD_NUMBER: _ClassVar[int]
    INSTITUTION_FIELD_NUMBER: _ClassVar[int]
    IS_REFERENCED_BY_COUNT_FIELD_NUMBER: _ClassVar[int]
    ISBN_FIELD_NUMBER: _ClassVar[int]
    ISBN_TYPE_FIELD_NUMBER: _ClassVar[int]
    ISSN_FIELD_NUMBER: _ClassVar[int]
    ISSN_TYPE_FIELD_NUMBER: _ClassVar[int]
    ISSUE_FIELD_NUMBER: _ClassVar[int]
    ISSUE_TITLE_FIELD_NUMBER: _ClassVar[int]
    ISSUED_FIELD_NUMBER: _ClassVar[int]
    JOURNAL_ISSUE_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    LICENSE_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    MEMBER_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_TITLE_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    PART_NUMBER_FIELD_NUMBER: _ClassVar[int]
    POSTED_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    PROCEEDINGS_SUBJECT_FIELD_NUMBER: _ClassVar[int]
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_ONLINE_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_OTHER_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_PRINT_FIELD_NUMBER: _ClassVar[int]
    PUBLISHER_FIELD_NUMBER: _ClassVar[int]
    PUBLISHER_LOCATION_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_COUNT_FIELD_NUMBER: _ClassVar[int]
    REFERENCES_COUNT_FIELD_NUMBER: _ClassVar[int]
    RELATION_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_FIELD_NUMBER: _ClassVar[int]
    REVIEW_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    SHORT_CONTAINER_TITLE_FIELD_NUMBER: _ClassVar[int]
    SHORT_TITLE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SPECIAL_NUMBERING_FIELD_NUMBER: _ClassVar[int]
    STANDARDS_BODY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SUBJECT_FIELD_NUMBER: _ClassVar[int]
    SUBTITLE_FIELD_NUMBER: _ClassVar[int]
    SUBTYPE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    TRANSLATOR_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    UPDATE_POLICY_FIELD_NUMBER: _ClassVar[int]
    UPDATE_TO_FIELD_NUMBER: _ClassVar[int]
    UPDATED_BY_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    abstract: str
    accepted: Date
    aliases: _containers.RepeatedScalarFieldContainer[str]
    alternative_id: _containers.RepeatedScalarFieldContainer[str]
    approved: Date
    archive: _containers.RepeatedScalarFieldContainer[str]
    article_number: str
    assertion: _containers.RepeatedCompositeFieldContainer[Assertion]
    author: _containers.RepeatedCompositeFieldContainer[Author]
    award: str
    award_start: Date
    chair: _containers.RepeatedCompositeFieldContainer[Author]
    clinical_trial_number: _containers.RepeatedCompositeFieldContainer[ClinicalTrial]
    component_number: str
    container_title: _containers.RepeatedScalarFieldContainer[str]
    content_created: Date
    content_domain: ContentDomain
    content_updated: Date
    contributor: _containers.RepeatedCompositeFieldContainer[Author]
    created: Date
    degree: _containers.RepeatedScalarFieldContainer[str]
    deposited: Date
    description: str
    doi: str
    edition_number: str
    editor: _containers.RepeatedCompositeFieldContainer[Author]
    event: Event
    free_to_read: FreeToRead
    funder: _containers.RepeatedCompositeFieldContainer[Funder]
    group_title: str
    indexed: Date
    institution: _containers.RepeatedCompositeFieldContainer[Affiliation]
    is_referenced_by_count: int
    isbn: _containers.RepeatedScalarFieldContainer[str]
    isbn_type: _containers.RepeatedCompositeFieldContainer[IdentifierType]
    issn: _containers.RepeatedScalarFieldContainer[str]
    issn_type: _containers.RepeatedCompositeFieldContainer[IdentifierType]
    issue: str
    issue_title: _containers.RepeatedScalarFieldContainer[str]
    issued: Date
    journal_issue: JournalIssue
    language: str
    license: _containers.RepeatedCompositeFieldContainer[License]
    link: _containers.RepeatedCompositeFieldContainer[Link]
    member: str
    original_title: _containers.RepeatedScalarFieldContainer[str]
    page: str
    part_number: str
    posted: Date
    prefix: str
    proceedings_subject: str
    project: _containers.RepeatedCompositeFieldContainer[Project]
    published: Date
    published_online: Date
    published_other: Date
    published_print: Date
    publisher: str
    publisher_location: str
    reference: _containers.RepeatedCompositeFieldContainer[Reference]
    reference_count: int
    references_count: int
    relation: _containers.MessageMap[str, RelationList]
    resource: Resource
    review: Review
    score: float
    short_container_title: _containers.RepeatedScalarFieldContainer[str]
    short_title: _containers.RepeatedScalarFieldContainer[str]
    source: str
    special_numbering: str
    standards_body: StandardsBody
    status: PostedContentStatus
    subject: _containers.RepeatedScalarFieldContainer[str]
    subtitle: _containers.RepeatedScalarFieldContainer[str]
    subtype: str
    title: _containers.RepeatedScalarFieldContainer[str]
    translator: _containers.RepeatedCompositeFieldContainer[Author]
    type: str
    update_policy: str
    update_to: _containers.RepeatedCompositeFieldContainer[Update]
    updated_by: _containers.RepeatedCompositeFieldContainer[Update]
    url: str
    version: VersionInfo
    volume: str
    def __init__(self, abstract: _Optional[str] = ..., accepted: _Optional[_Union[Date, _Mapping]] = ..., aliases: _Optional[_Iterable[str]] = ..., alternative_id: _Optional[_Iterable[str]] = ..., approved: _Optional[_Union[Date, _Mapping]] = ..., archive: _Optional[_Iterable[str]] = ..., article_number: _Optional[str] = ..., assertion: _Optional[_Iterable[_Union[Assertion, _Mapping]]] = ..., author: _Optional[_Iterable[_Union[Author, _Mapping]]] = ..., award: _Optional[str] = ..., award_start: _Optional[_Union[Date, _Mapping]] = ..., chair: _Optional[_Iterable[_Union[Author, _Mapping]]] = ..., clinical_trial_number: _Optional[_Iterable[_Union[ClinicalTrial, _Mapping]]] = ..., component_number: _Optional[str] = ..., container_title: _Optional[_Iterable[str]] = ..., content_created: _Optional[_Union[Date, _Mapping]] = ..., content_domain: _Optional[_Union[ContentDomain, _Mapping]] = ..., content_updated: _Optional[_Union[Date, _Mapping]] = ..., contributor: _Optional[_Iterable[_Union[Author, _Mapping]]] = ..., created: _Optional[_Union[Date, _Mapping]] = ..., degree: _Optional[_Iterable[str]] = ..., deposited: _Optional[_Union[Date, _Mapping]] = ..., description: _Optional[str] = ..., doi: _Optional[str] = ..., edition_number: _Optional[str] = ..., editor: _Optional[_Iterable[_Union[Author, _Mapping]]] = ..., event: _Optional[_Union[Event, _Mapping]] = ..., free_to_read: _Optional[_Union[FreeToRead, _Mapping]] = ..., funder: _Optional[_Iterable[_Union[Funder, _Mapping]]] = ..., group_title: _Optional[str] = ..., indexed: _Optional[_Union[Date, _Mapping]] = ..., institution: _Optional[_Iterable[_Union[Affiliation, _Mapping]]] = ..., is_referenced_by_count: _Optional[int] = ..., isbn: _Optional[_Iterable[str]] = ..., isbn_type: _Optional[_Iterable[_Union[IdentifierType, _Mapping]]] = ..., issn: _Optional[_Iterable[str]] = ..., issn_type: _Optional[_Iterable[_Union[IdentifierType, _Mapping]]] = ..., issue: _Optional[str] = ..., issue_title: _Optional[_Iterable[str]] = ..., issued: _Optional[_Union[Date, _Mapping]] = ..., journal_issue: _Optional[_Union[JournalIssue, _Mapping]] = ..., language: _Optional[str] = ..., license: _Optional[_Iterable[_Union[License, _Mapping]]] = ..., link: _Optional[_Iterable[_Union[Link, _Mapping]]] = ..., member: _Optional[str] = ..., original_title: _Optional[_Iterable[str]] = ..., page: _Optional[str] = ..., part_number: _Optional[str] = ..., posted: _Optional[_Union[Date, _Mapping]] = ..., prefix: _Optional[str] = ..., proceedings_subject: _Optional[str] = ..., project: _Optional[_Iterable[_Union[Project, _Mapping]]] = ..., published: _Optional[_Union[Date, _Mapping]] = ..., published_online: _Optional[_Union[Date, _Mapping]] = ..., published_other: _Optional[_Union[Date, _Mapping]] = ..., published_print: _Optional[_Union[Date, _Mapping]] = ..., publisher: _Optional[str] = ..., publisher_location: _Optional[str] = ..., reference: _Optional[_Iterable[_Union[Reference, _Mapping]]] = ..., reference_count: _Optional[int] = ..., references_count: _Optional[int] = ..., relation: _Optional[_Mapping[str, RelationList]] = ..., resource: _Optional[_Union[Resource, _Mapping]] = ..., review: _Optional[_Union[Review, _Mapping]] = ..., score: _Optional[float] = ..., short_container_title: _Optional[_Iterable[str]] = ..., short_title: _Optional[_Iterable[str]] = ..., source: _Optional[str] = ..., special_numbering: _Optional[str] = ..., standards_body: _Optional[_Union[StandardsBody, _Mapping]] = ..., status: _Optional[_Union[PostedContentStatus, _Mapping]] = ..., subject: _Optional[_Iterable[str]] = ..., subtitle: _Optional[_Iterable[str]] = ..., subtype: _Optional[str] = ..., title: _Optional[_Iterable[str]] = ..., translator: _Optional[_Iterable[_Union[Author, _Mapping]]] = ..., type: _Optional[str] = ..., update_policy: _Optional[str] = ..., update_to: _Optional[_Iterable[_Union[Update, _Mapping]]] = ..., updated_by: _Optional[_Iterable[_Union[Update, _Mapping]]] = ..., url: _Optional[str] = ..., version: _Optional[_Union[VersionInfo, _Mapping]] = ..., volume: _Optional[str] = ...) -> None: ...

class Date(_message.Message):
    __slots__ = ("date_parts", "date_time", "timestamp", "version")
    DATE_PARTS_FIELD_NUMBER: _ClassVar[int]
    DATE_TIME_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    date_parts: _containers.RepeatedCompositeFieldContainer[DateParts]
    date_time: str
    timestamp: int
    version: str
    def __init__(self, date_parts: _Optional[_Iterable[_Union[DateParts, _Mapping]]] = ..., date_time: _Optional[str] = ..., timestamp: _Optional[int] = ..., version: _Optional[str] = ...) -> None: ...

class DateParts(_message.Message):
    __slots__ = ("parts",)
    PARTS_FIELD_NUMBER: _ClassVar[int]
    parts: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, parts: _Optional[_Iterable[int]] = ...) -> None: ...

class Affiliation(_message.Message):
    __slots__ = ("acronym", "department", "id", "name", "place")
    ACRONYM_FIELD_NUMBER: _ClassVar[int]
    DEPARTMENT_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PLACE_FIELD_NUMBER: _ClassVar[int]
    acronym: _containers.RepeatedScalarFieldContainer[str]
    department: _containers.RepeatedScalarFieldContainer[str]
    id: _containers.RepeatedCompositeFieldContainer[AffiliationIdentifier]
    name: str
    place: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, acronym: _Optional[_Iterable[str]] = ..., department: _Optional[_Iterable[str]] = ..., id: _Optional[_Iterable[_Union[AffiliationIdentifier, _Mapping]]] = ..., name: _Optional[str] = ..., place: _Optional[_Iterable[str]] = ...) -> None: ...

class AffiliationIdentifier(_message.Message):
    __slots__ = ("asserted_by", "id", "id_type")
    ASSERTED_BY_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    ID_TYPE_FIELD_NUMBER: _ClassVar[int]
    asserted_by: str
    id: str
    id_type: str
    def __init__(self, asserted_by: _Optional[str] = ..., id: _Optional[str] = ..., id_type: _Optional[str] = ...) -> None: ...

class Assertion(_message.Message):
    __slots__ = ("explanation", "group", "label", "name", "order", "url", "value")
    EXPLANATION_FIELD_NUMBER: _ClassVar[int]
    GROUP_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ORDER_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    explanation: AssertionExplanation
    group: AssertionGroup
    label: str
    name: str
    order: int
    url: str
    value: str
    def __init__(self, explanation: _Optional[_Union[AssertionExplanation, _Mapping]] = ..., group: _Optional[_Union[AssertionGroup, _Mapping]] = ..., label: _Optional[str] = ..., name: _Optional[str] = ..., order: _Optional[int] = ..., url: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class AssertionExplanation(_message.Message):
    __slots__ = ("url",)
    URL_FIELD_NUMBER: _ClassVar[int]
    url: str
    def __init__(self, url: _Optional[str] = ...) -> None: ...

class AssertionGroup(_message.Message):
    __slots__ = ("label", "name")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    label: str
    name: str
    def __init__(self, label: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class Author(_message.Message):
    __slots__ = ("affiliation", "authenticated_orcid", "family", "given", "name", "orcid", "prefix", "role", "sequence", "suffix")
    AFFILIATION_FIELD_NUMBER: _ClassVar[int]
    AUTHENTICATED_ORCID_FIELD_NUMBER: _ClassVar[int]
    FAMILY_FIELD_NUMBER: _ClassVar[int]
    GIVEN_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ORCID_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    SUFFIX_FIELD_NUMBER: _ClassVar[int]
    affiliation: _containers.RepeatedCompositeFieldContainer[Affiliation]
    authenticated_orcid: bool
    family: str
    given: str
    name: str
    orcid: str
    prefix: str
    role: _containers.RepeatedCompositeFieldContainer[ContributorRole]
    sequence: str
    suffix: str
    def __init__(self, affiliation: _Optional[_Iterable[_Union[Affiliation, _Mapping]]] = ..., authenticated_orcid: _Optional[bool] = ..., family: _Optional[str] = ..., given: _Optional[str] = ..., name: _Optional[str] = ..., orcid: _Optional[str] = ..., prefix: _Optional[str] = ..., role: _Optional[_Iterable[_Union[ContributorRole, _Mapping]]] = ..., sequence: _Optional[str] = ..., suffix: _Optional[str] = ...) -> None: ...

class ContributorRole(_message.Message):
    __slots__ = ("role", "vocabulary")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    VOCABULARY_FIELD_NUMBER: _ClassVar[int]
    role: str
    vocabulary: str
    def __init__(self, role: _Optional[str] = ..., vocabulary: _Optional[str] = ...) -> None: ...

class ClinicalTrial(_message.Message):
    __slots__ = ("clinical_trial_number", "registry", "type")
    CLINICAL_TRIAL_NUMBER_FIELD_NUMBER: _ClassVar[int]
    REGISTRY_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    clinical_trial_number: str
    registry: str
    type: str
    def __init__(self, clinical_trial_number: _Optional[str] = ..., registry: _Optional[str] = ..., type: _Optional[str] = ...) -> None: ...

class ContentDomain(_message.Message):
    __slots__ = ("crossmark_restriction", "domain")
    CROSSMARK_RESTRICTION_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_FIELD_NUMBER: _ClassVar[int]
    crossmark_restriction: bool
    domain: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, crossmark_restriction: _Optional[bool] = ..., domain: _Optional[_Iterable[str]] = ...) -> None: ...

class Event(_message.Message):
    __slots__ = ("acronym", "end", "location", "name", "number", "sponsor", "start", "theme")
    ACRONYM_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NUMBER_FIELD_NUMBER: _ClassVar[int]
    SPONSOR_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    THEME_FIELD_NUMBER: _ClassVar[int]
    acronym: str
    end: Date
    location: str
    name: str
    number: str
    sponsor: _containers.RepeatedScalarFieldContainer[str]
    start: Date
    theme: str
    def __init__(self, acronym: _Optional[str] = ..., end: _Optional[_Union[Date, _Mapping]] = ..., location: _Optional[str] = ..., name: _Optional[str] = ..., number: _Optional[str] = ..., sponsor: _Optional[_Iterable[str]] = ..., start: _Optional[_Union[Date, _Mapping]] = ..., theme: _Optional[str] = ...) -> None: ...

class FreeToRead(_message.Message):
    __slots__ = ("end_date", "start_date")
    END_DATE_FIELD_NUMBER: _ClassVar[int]
    START_DATE_FIELD_NUMBER: _ClassVar[int]
    end_date: Date
    start_date: Date
    def __init__(self, end_date: _Optional[_Union[Date, _Mapping]] = ..., start_date: _Optional[_Union[Date, _Mapping]] = ...) -> None: ...

class Funder(_message.Message):
    __slots__ = ("award", "award_info", "doi", "doi_asserted_by", "id", "name")
    AWARD_FIELD_NUMBER: _ClassVar[int]
    AWARD_INFO_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    DOI_ASSERTED_BY_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    award: _containers.RepeatedScalarFieldContainer[str]
    award_info: _containers.RepeatedCompositeFieldContainer[AwardInfo]
    doi: str
    doi_asserted_by: str
    id: _containers.RepeatedCompositeFieldContainer[FunderIdentifier]
    name: str
    def __init__(self, award: _Optional[_Iterable[str]] = ..., award_info: _Optional[_Iterable[_Union[AwardInfo, _Mapping]]] = ..., doi: _Optional[str] = ..., doi_asserted_by: _Optional[str] = ..., id: _Optional[_Iterable[_Union[FunderIdentifier, _Mapping]]] = ..., name: _Optional[str] = ...) -> None: ...

class AwardInfo(_message.Message):
    __slots__ = ("award_number",)
    AWARD_NUMBER_FIELD_NUMBER: _ClassVar[int]
    award_number: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, award_number: _Optional[_Iterable[str]] = ...) -> None: ...

class FunderIdentifier(_message.Message):
    __slots__ = ("asserted_by", "id", "id_type")
    ASSERTED_BY_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    ID_TYPE_FIELD_NUMBER: _ClassVar[int]
    asserted_by: str
    id: str
    id_type: str
    def __init__(self, asserted_by: _Optional[str] = ..., id: _Optional[str] = ..., id_type: _Optional[str] = ...) -> None: ...

class IdentifierType(_message.Message):
    __slots__ = ("type", "value")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    type: str
    value: str
    def __init__(self, type: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class JournalIssue(_message.Message):
    __slots__ = ("issue", "published_online", "published_print")
    ISSUE_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_ONLINE_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_PRINT_FIELD_NUMBER: _ClassVar[int]
    issue: str
    published_online: Date
    published_print: Date
    def __init__(self, issue: _Optional[str] = ..., published_online: _Optional[_Union[Date, _Mapping]] = ..., published_print: _Optional[_Union[Date, _Mapping]] = ...) -> None: ...

class License(_message.Message):
    __slots__ = ("content_version", "delay_in_days", "start", "url")
    CONTENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    DELAY_IN_DAYS_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    content_version: str
    delay_in_days: int
    start: Date
    url: str
    def __init__(self, content_version: _Optional[str] = ..., delay_in_days: _Optional[int] = ..., start: _Optional[_Union[Date, _Mapping]] = ..., url: _Optional[str] = ...) -> None: ...

class Link(_message.Message):
    __slots__ = ("content_type", "content_version", "intended_application", "url")
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    INTENDED_APPLICATION_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    content_type: str
    content_version: str
    intended_application: str
    url: str
    def __init__(self, content_type: _Optional[str] = ..., content_version: _Optional[str] = ..., intended_application: _Optional[str] = ..., url: _Optional[str] = ...) -> None: ...

class Project(_message.Message):
    __slots__ = ("award_amount", "award_end", "award_planned_end", "award_planned_start", "award_start", "co_lead_investigator", "funding", "investigator", "lead_investigator", "project_description", "project_title")
    AWARD_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    AWARD_END_FIELD_NUMBER: _ClassVar[int]
    AWARD_PLANNED_END_FIELD_NUMBER: _ClassVar[int]
    AWARD_PLANNED_START_FIELD_NUMBER: _ClassVar[int]
    AWARD_START_FIELD_NUMBER: _ClassVar[int]
    CO_LEAD_INVESTIGATOR_FIELD_NUMBER: _ClassVar[int]
    FUNDING_FIELD_NUMBER: _ClassVar[int]
    INVESTIGATOR_FIELD_NUMBER: _ClassVar[int]
    LEAD_INVESTIGATOR_FIELD_NUMBER: _ClassVar[int]
    PROJECT_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PROJECT_TITLE_FIELD_NUMBER: _ClassVar[int]
    award_amount: AwardAmount
    award_end: Date
    award_planned_end: Date
    award_planned_start: Date
    award_start: Date
    co_lead_investigator: _containers.RepeatedCompositeFieldContainer[Investigator]
    funding: _containers.RepeatedCompositeFieldContainer[Funding]
    investigator: _containers.RepeatedCompositeFieldContainer[Investigator]
    lead_investigator: _containers.RepeatedCompositeFieldContainer[Investigator]
    project_description: _containers.RepeatedCompositeFieldContainer[ProjectDescription]
    project_title: _containers.RepeatedCompositeFieldContainer[ProjectTitle]
    def __init__(self, award_amount: _Optional[_Union[AwardAmount, _Mapping]] = ..., award_end: _Optional[_Union[Date, _Mapping]] = ..., award_planned_end: _Optional[_Union[Date, _Mapping]] = ..., award_planned_start: _Optional[_Union[Date, _Mapping]] = ..., award_start: _Optional[_Union[Date, _Mapping]] = ..., co_lead_investigator: _Optional[_Iterable[_Union[Investigator, _Mapping]]] = ..., funding: _Optional[_Iterable[_Union[Funding, _Mapping]]] = ..., investigator: _Optional[_Iterable[_Union[Investigator, _Mapping]]] = ..., lead_investigator: _Optional[_Iterable[_Union[Investigator, _Mapping]]] = ..., project_description: _Optional[_Iterable[_Union[ProjectDescription, _Mapping]]] = ..., project_title: _Optional[_Iterable[_Union[ProjectTitle, _Mapping]]] = ...) -> None: ...

class AwardAmount(_message.Message):
    __slots__ = ("amount", "currency", "percentage")
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    CURRENCY_FIELD_NUMBER: _ClassVar[int]
    PERCENTAGE_FIELD_NUMBER: _ClassVar[int]
    amount: float
    currency: str
    percentage: int
    def __init__(self, amount: _Optional[float] = ..., currency: _Optional[str] = ..., percentage: _Optional[int] = ...) -> None: ...

class Funding(_message.Message):
    __slots__ = ("award_amount", "funder", "scheme", "type")
    AWARD_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    FUNDER_FIELD_NUMBER: _ClassVar[int]
    SCHEME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    award_amount: AwardAmount
    funder: Funder
    scheme: str
    type: str
    def __init__(self, award_amount: _Optional[_Union[AwardAmount, _Mapping]] = ..., funder: _Optional[_Union[Funder, _Mapping]] = ..., scheme: _Optional[str] = ..., type: _Optional[str] = ...) -> None: ...

class Investigator(_message.Message):
    __slots__ = ("affiliation", "alternate_name", "authenticated_orcid", "family", "given", "name", "orcid", "prefix", "role_end", "role_start", "sequence", "suffix")
    AFFILIATION_FIELD_NUMBER: _ClassVar[int]
    ALTERNATE_NAME_FIELD_NUMBER: _ClassVar[int]
    AUTHENTICATED_ORCID_FIELD_NUMBER: _ClassVar[int]
    FAMILY_FIELD_NUMBER: _ClassVar[int]
    GIVEN_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ORCID_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    ROLE_END_FIELD_NUMBER: _ClassVar[int]
    ROLE_START_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    SUFFIX_FIELD_NUMBER: _ClassVar[int]
    affiliation: _containers.RepeatedCompositeFieldContainer[InvestigatorAffiliation]
    alternate_name: _containers.RepeatedScalarFieldContainer[str]
    authenticated_orcid: bool
    family: str
    given: str
    name: str
    orcid: str
    prefix: str
    role_end: Date
    role_start: Date
    sequence: str
    suffix: str
    def __init__(self, affiliation: _Optional[_Iterable[_Union[InvestigatorAffiliation, _Mapping]]] = ..., alternate_name: _Optional[_Iterable[str]] = ..., authenticated_orcid: _Optional[bool] = ..., family: _Optional[str] = ..., given: _Optional[str] = ..., name: _Optional[str] = ..., orcid: _Optional[str] = ..., prefix: _Optional[str] = ..., role_end: _Optional[_Union[Date, _Mapping]] = ..., role_start: _Optional[_Union[Date, _Mapping]] = ..., sequence: _Optional[str] = ..., suffix: _Optional[str] = ...) -> None: ...

class InvestigatorAffiliation(_message.Message):
    __slots__ = ("country", "id", "name")
    COUNTRY_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    country: str
    id: _containers.RepeatedCompositeFieldContainer[AffiliationIdentifier]
    name: str
    def __init__(self, country: _Optional[str] = ..., id: _Optional[_Iterable[_Union[AffiliationIdentifier, _Mapping]]] = ..., name: _Optional[str] = ...) -> None: ...

class ProjectDescription(_message.Message):
    __slots__ = ("description", "language")
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    description: str
    language: str
    def __init__(self, description: _Optional[str] = ..., language: _Optional[str] = ...) -> None: ...

class ProjectTitle(_message.Message):
    __slots__ = ("language", "title")
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    language: str
    title: str
    def __init__(self, language: _Optional[str] = ..., title: _Optional[str] = ...) -> None: ...

class Reference(_message.Message):
    __slots__ = ("article_title", "author", "component", "doi", "doi_asserted_by", "edition", "first_page", "isbn", "isbn_type", "issn", "issn_type", "issue", "journal_title", "key", "series_title", "standard_designator", "standards_body", "type", "unstructured", "volume", "volume_title", "year")
    ARTICLE_TITLE_FIELD_NUMBER: _ClassVar[int]
    AUTHOR_FIELD_NUMBER: _ClassVar[int]
    COMPONENT_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    DOI_ASSERTED_BY_FIELD_NUMBER: _ClassVar[int]
    EDITION_FIELD_NUMBER: _ClassVar[int]
    FIRST_PAGE_FIELD_NUMBER: _ClassVar[int]
    ISBN_FIELD_NUMBER: _ClassVar[int]
    ISBN_TYPE_FIELD_NUMBER: _ClassVar[int]
    ISSN_FIELD_NUMBER: _ClassVar[int]
    ISSN_TYPE_FIELD_NUMBER: _ClassVar[int]
    ISSUE_FIELD_NUMBER: _ClassVar[int]
    JOURNAL_TITLE_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    SERIES_TITLE_FIELD_NUMBER: _ClassVar[int]
    STANDARD_DESIGNATOR_FIELD_NUMBER: _ClassVar[int]
    STANDARDS_BODY_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    UNSTRUCTURED_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    VOLUME_TITLE_FIELD_NUMBER: _ClassVar[int]
    YEAR_FIELD_NUMBER: _ClassVar[int]
    article_title: str
    author: str
    component: str
    doi: str
    doi_asserted_by: str
    edition: str
    first_page: str
    isbn: str
    isbn_type: str
    issn: str
    issn_type: str
    issue: str
    journal_title: str
    key: str
    series_title: str
    standard_designator: str
    standards_body: str
    type: str
    unstructured: str
    volume: str
    volume_title: str
    year: str
    def __init__(self, article_title: _Optional[str] = ..., author: _Optional[str] = ..., component: _Optional[str] = ..., doi: _Optional[str] = ..., doi_asserted_by: _Optional[str] = ..., edition: _Optional[str] = ..., first_page: _Optional[str] = ..., isbn: _Optional[str] = ..., isbn_type: _Optional[str] = ..., issn: _Optional[str] = ..., issn_type: _Optional[str] = ..., issue: _Optional[str] = ..., journal_title: _Optional[str] = ..., key: _Optional[str] = ..., series_title: _Optional[str] = ..., standard_designator: _Optional[str] = ..., standards_body: _Optional[str] = ..., type: _Optional[str] = ..., unstructured: _Optional[str] = ..., volume: _Optional[str] = ..., volume_title: _Optional[str] = ..., year: _Optional[str] = ...) -> None: ...

class RelationList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[RelationObject]
    def __init__(self, items: _Optional[_Iterable[_Union[RelationObject, _Mapping]]] = ...) -> None: ...

class RelationObject(_message.Message):
    __slots__ = ("asserted_by", "id", "id_type")
    ASSERTED_BY_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    ID_TYPE_FIELD_NUMBER: _ClassVar[int]
    asserted_by: str
    id: str
    id_type: str
    def __init__(self, asserted_by: _Optional[str] = ..., id: _Optional[str] = ..., id_type: _Optional[str] = ...) -> None: ...

class Resource(_message.Message):
    __slots__ = ("primary", "secondary")
    PRIMARY_FIELD_NUMBER: _ClassVar[int]
    SECONDARY_FIELD_NUMBER: _ClassVar[int]
    primary: PrimaryResource
    secondary: _containers.RepeatedCompositeFieldContainer[SecondaryResource]
    def __init__(self, primary: _Optional[_Union[PrimaryResource, _Mapping]] = ..., secondary: _Optional[_Iterable[_Union[SecondaryResource, _Mapping]]] = ...) -> None: ...

class PrimaryResource(_message.Message):
    __slots__ = ("url",)
    URL_FIELD_NUMBER: _ClassVar[int]
    url: str
    def __init__(self, url: _Optional[str] = ...) -> None: ...

class SecondaryResource(_message.Message):
    __slots__ = ("label", "url")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    label: str
    url: str
    def __init__(self, label: _Optional[str] = ..., url: _Optional[str] = ...) -> None: ...

class Review(_message.Message):
    __slots__ = ("competing_interest_statement", "language", "recommendation", "revision_round", "running_number", "stage", "type")
    COMPETING_INTEREST_STATEMENT_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    RECOMMENDATION_FIELD_NUMBER: _ClassVar[int]
    REVISION_ROUND_FIELD_NUMBER: _ClassVar[int]
    RUNNING_NUMBER_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    competing_interest_statement: str
    language: str
    recommendation: str
    revision_round: str
    running_number: str
    stage: str
    type: str
    def __init__(self, competing_interest_statement: _Optional[str] = ..., language: _Optional[str] = ..., recommendation: _Optional[str] = ..., revision_round: _Optional[str] = ..., running_number: _Optional[str] = ..., stage: _Optional[str] = ..., type: _Optional[str] = ...) -> None: ...

class StandardsBody(_message.Message):
    __slots__ = ("acronym", "name")
    ACRONYM_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    acronym: str
    name: str
    def __init__(self, acronym: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class PostedContentStatus(_message.Message):
    __slots__ = ("status_description", "type", "update")
    STATUS_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    UPDATE_FIELD_NUMBER: _ClassVar[int]
    status_description: _containers.RepeatedCompositeFieldContainer[StatusDescription]
    type: str
    update: Date
    def __init__(self, status_description: _Optional[_Iterable[_Union[StatusDescription, _Mapping]]] = ..., type: _Optional[str] = ..., update: _Optional[_Union[Date, _Mapping]] = ...) -> None: ...

class StatusDescription(_message.Message):
    __slots__ = ("description", "language")
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    description: str
    language: str
    def __init__(self, description: _Optional[str] = ..., language: _Optional[str] = ...) -> None: ...

class Update(_message.Message):
    __slots__ = ("doi", "label", "record_id", "source", "type", "updated")
    DOI_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    RECORD_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_FIELD_NUMBER: _ClassVar[int]
    doi: str
    label: str
    record_id: str
    source: str
    type: str
    updated: Date
    def __init__(self, doi: _Optional[str] = ..., label: _Optional[str] = ..., record_id: _Optional[str] = ..., source: _Optional[str] = ..., type: _Optional[str] = ..., updated: _Optional[_Union[Date, _Mapping]] = ...) -> None: ...

class VersionInfo(_message.Message):
    __slots__ = ("language", "version", "version_description")
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    VERSION_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    language: str
    version: str
    version_description: _containers.RepeatedCompositeFieldContainer[VersionInfoDescription]
    def __init__(self, language: _Optional[str] = ..., version: _Optional[str] = ..., version_description: _Optional[_Iterable[_Union[VersionInfoDescription, _Mapping]]] = ...) -> None: ...

class VersionInfoDescription(_message.Message):
    __slots__ = ("description", "language")
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    description: str
    language: str
    def __init__(self, description: _Optional[str] = ..., language: _Optional[str] = ...) -> None: ...
