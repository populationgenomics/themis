r"""Fill a paper's `Metadata` from the metadata's source record, the index records it holds.

`metadata.pb` holds the paper's `Metadata` (title, authors, year, container title, abstract, MeSH headings, publication
types) beside the `MetadataSource` it is filled from (docs/design/litcache-manifest.md, "The bibliographic record").
`fill` reads all seven from the resolving index's record, PubMed's or Crossref's, or OpenAlex's where it is the only
record; where OpenAlex's record sits beside another, the whole author list comes from it instead, and every other field
still from the resolving record. Ingest, the metadata refresh and the fill of stored records all call it, so a field is
read from each index in one place. Where each comes from:

- PubMed (typed record): `title` the `ArticleTitle`, for a book its chapter's or else the book's; `authors` each
  `ForeName LastName` (initials where no fore name), or the `CollectiveName`, its `orcid` the first well-formed
  `Identifier` with `Source="ORCID"`; `year` the journal issue's `PubDate`, a chapter's `ContributionDate` (else its
  book's `PubDate`), or a whole book's `PubDate`; `container_title` the `Journal/Title`, or a chapter's `BookTitle`;
  `abstract` its `AbstractText` sections, each as `Label: text`; `mesh_headings` an article's `MeshHeadingList`, each
  `DescriptorName` and its `QualifierName`s by `UI` and `MajorTopicYN` (a book states none); `publication_types` an
  article's `PublicationTypeList`, or a book's `PublicationType`s, each name once.
- Crossref (JSON): `title` the first non-empty `title`; `authors` each `given family`, or the `name` of an author with
  no family name, or the `given` name of an author stating only that, its `orcid` the author's `ORCID`; `year`
  `issued.date-parts[0][0]`; `container_title` the first non-empty `container-title`; `abstract` the JATS `abstract`,
  each section title kept as `Title: ` and a title that only says "Abstract" dropped; `publication_types` the `type`,
  mapped by `_CROSSREF_PUBLICATION_TYPES` with its `subtype`. It states no `mesh_headings`.
- OpenAlex (JSON): `title` the `title`; `authors` each `authorships[].author.display_name`, its `orcid` the
  `author.orcid`, the iD of the profile OpenAlex groups the authorship into (the authorship's `raw_orcid`, the iD
  deposited with the work, is not read); `year` the `publication_year`; `container_title` the
  `primary_location.source.display_name`; `abstract` the `abstract_inverted_index`, its words put back in order;
  `mesh_headings` the `mesh` rows grouped back into headings (`_openalex_mesh_headings`); `publication_types` the
  `type`, mapped by `_OPENALEX_PUBLICATION_TYPES`.

Every text field holds plain text, whichever index answered, with its whitespace collapsed. Crossref's and OpenAlex's
strings go through an HTML parser fed only the tags of known markup (see `_is_markup`); every other `<` is text, so
`p < 0.05`, `a<b c` and `c.1A>G` stay as they are. Each
entity is decoded once (only a reference closed by `;`, as XML has it, so `&para` in `&parameter` stays text), and a
block element (a paragraph, a section, a break, a table cell) separates words. Markup escaped twice upstream
(`&lt;i&gt;`) reads as a tag once decoded, so the decoded text is stripped a second time, of recognised markup alone.
The PubMed converter renders some fields as markdown (a title, an abstract section, a collective name, a book title);
those are undone by the converter's own inverse, `xml_converter.markdown_to_plain_text`, which also reads back a marker
the record states literally (`p.R391\*`). The fields it copies as they are (a journal title, an author's name parts,
an abstract section's label, a publication type) only have their whitespace collapsed. OpenAlex's rebuilt abstract is
plain text already and goes through neither; where it starts with an "Abstract" heading, the word followed by a colon or
a full stop, or by a capitalised word ("Abstract reasoning" stays), the heading is dropped.

A field the record does not state is empty. PubMed's converter reads a date into a timestamp from its `Year`, or else
the year a `MedlineDate` starts with, and gives year 1 when neither parses; year 1 is read as no year. An author entry
stating no name is left out, in every index; a name of the wrong JSON type is drift. A PubMed author marked invalid
(`ValidYN="N"`) is left out, and so is an author list of editors. A whole book has no container. An ORCID iD is held
in its bare form, an `orcid.org` URL's prefix and surrounding whitespace dropped and a check digit `x` uppercased; one
malformed, or whose ISO 7064 check digit fails, is read as none, and an iD of the wrong JSON type is drift. A Crossref
or OpenAlex type the table does not map, such as a book chapter, is read as no publication type, and a type of the
wrong JSON type is drift. A field every record of its index states is read as required, so its absence is drift too:
OpenAlex's `title`, and each `mesh` row's `descriptor_ui` (which must not be null), `qualifier_ui` (null for a
descriptor's own row) and `is_major_topic`.
"""

from __future__ import annotations

import html
import html.parser
import re
from collections.abc import Callable, Iterable, Mapping
from typing import override

from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2, xml_converter

from themis.litcache import sourced_metadata, upstream_record
from themis.litcache.models import litcache_pb2

_AuthorList = pubmed_pb2.AuthorList
_Author = litcache_pb2.Metadata.Author
_MeshHeading = litcache_pb2.Metadata.MeshHeading
_MeshTerm = litcache_pb2.Metadata.MeshTerm
# The year the PubMed converter gives a date it could not parse.
_UNPARSED_YEAR = 1
# The years `Metadata.year` can hold: four digits.
_FIRST_YEAR, _LAST_YEAR = 1000, 9999
_SPACE = re.compile(r'\s+')
# The markup Crossref's and OpenAlex's strings carry, named with or without a namespace prefix (`i`, `jats:italic`); any
# element under a `jats` or `mml` prefix (`jats1:p` too) is markup as well. A `<…>` naming anything else is text:
# `a<b and c>d`, `<Na:K>`.
_MARKUP_ELEMENTS = frozenset(
    {
        'a',
        'abbr',
        'abbrev',
        'abstract',
        'alternatives',
        'b',
        'big',
        'blockquote',
        'bold',
        'br',
        'break',
        'caption',
        'center',
        'cite',
        'code',
        'dd',
        'del',
        'disp-formula',
        'disp-quote',
        'div',
        'dl',
        'dt',
        'em',
        'email',
        'ext-link',
        'fig',
        'font',
        'graphic',
        'h1',
        'h2',
        'h3',
        'h4',
        'h5',
        'h6',
        'hr',
        'i',
        'img',
        'inf',
        'inline-formula',
        'inline-graphic',
        'ins',
        'italic',
        'kbd',
        'label',
        'li',
        'list',
        'list-item',
        'mark',
        'monospace',
        'named-content',
        'ol',
        'overline',
        'p',
        'pre',
        'q',
        'roman',
        's',
        'samp',
        'sans-serif',
        'sc',
        'scp',
        'sec',
        'small',
        'smlcap',
        'span',
        'strike',
        'strong',
        'styled-content',
        'sub',
        'subtitle',
        'sup',
        'table',
        'td',
        'tex-math',
        'th',
        'title',
        'tr',
        'tt',
        'u',
        'ul',
        'underline',
        'uri',
        'var',
        'xref',
    }
)
# MathML's presentation elements, which a formula carries under any prefix (`a:math`, `f:mi`) or none.
_MATHML_ELEMENTS = frozenset(
    {
        'annotation',
        'annotation-xml',
        'maction',
        'maligngroup',
        'malignmark',
        'math',
        'menclose',
        'merror',
        'mfenced',
        'mfrac',
        'mglyph',
        'mi',
        'mlabeledtr',
        'mmultiscripts',
        'mn',
        'mo',
        'mover',
        'mpadded',
        'mphantom',
        'mprescripts',
        'mroot',
        'mrow',
        'ms',
        'mspace',
        'msqrt',
        'mstyle',
        'msub',
        'msubsup',
        'msup',
        'mtable',
        'mtd',
        'mtext',
        'mtr',
        'munder',
        'munderover',
        'none',
        'semantics',
    }
)
# Elements that separate words (a JATS name without its `jats:` prefix); any other tag joins its text to its neighbours.
_BLOCK_ELEMENTS = frozenset(
    {
        'blockquote',
        'br',
        'break',
        'caption',
        'center',
        'dd',
        'div',
        'dl',
        'dt',
        'h1',
        'h2',
        'h3',
        'h4',
        'h5',
        'h6',
        'hr',
        'label',
        'li',
        'list',
        'list-item',
        'p',
        'pre',
        'sec',
        'subtitle',
        'table',
        'table-wrap',
        'td',
        'th',
        'tr',
    }
)
# A heading, not prose: "Abstract" then a colon or full stop, or a capitalised word ("Abstract reasoning" stays).
_LEADING_ABSTRACT = re.compile(r'^(?:ABSTRACT|Abstract)(?:\s*[:.]\s*|\s+(?=[A-Z]))')
# A start or end tag whose attributes each carry a value; it is markup if its name is too (`_is_markup`).
_TAG = re.compile(
    r'</?(?:(?P<prefix>[A-Za-z][\w.-]*):)?(?P<name>[A-Za-z][\w.-]*)'
    r'(?:(?:\s+|(?<=["\']))[A-Za-z_:][\w.:-]*\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'<>=`]+))*\s*/?>'
)
# A prefix whose every element is markup: `jats`, `jats1`, `mml`.
_MARKUP_PREFIX = re.compile(r'jats\d*|mml')
# Elements whose content html.parser reads as raw text since Python 3.13.6; under a prefix each is an ordinary element.
_RAW_TEXT_ELEMENTS = frozenset(
    {'iframe', 'noembed', 'noframes', 'plaintext', 'script', 'style', 'textarea', 'title', 'xmp'}
)
_CDATA_START, _CDATA_END = '<![CDATA[', ']]>'
# An `&` that does not open a reference closed by `;`; html.unescape would decode a legacy name without one.
_UNTERMINATED_AMPERSAND = re.compile(r'&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);)')
# An ORCID iD as a record states it: bare, or as an orcid.org URL, its check digit `X` in either case.
_STATED_ORCID = re.compile(r'(?:https?://orcid\.org/)?([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9Xx])')
_ORCID_SOURCE = 'ORCID'
# The NLM Publication Type each OpenAlex `type` that has one maps to.
_OPENALEX_PUBLICATION_TYPES = {
    'article': 'Journal Article',
    'review': 'Review',
    'preprint': 'Preprint',
    'letter': 'Letter',
    'erratum': 'Published Erratum',
    'retraction': 'Retraction Notice',
}
# The NLM Publication Type each Crossref `type` and `subtype` that has one maps to; a `None` subtype stands for any.
_CROSSREF_PUBLICATION_TYPES = {
    ('journal-article', None): 'Journal Article',
    ('posted-content', 'preprint'): 'Preprint',
}


def fill(source: litcache_pb2.MetadataSource) -> litcache_pb2.Metadata:
    """The paper's `Metadata`, filled from the index records its source record holds (see `read`).

    Raises:
        upstream_record.SchemaDriftError: If a field a reader reads has a shape it cannot read, or the metadata it
            fills breaks the rules `Metadata` declares.
        ValueError: If the source record holds no index record to fill from.
    """
    metadata = read(source)
    try:
        sourced_metadata.check_metadata(metadata)
    except ValueError as e:  # a reader let through a value the proto refuses: drift, charged to the paper
        raise upstream_record.SchemaDriftError(_index(source), f'the filled metadata breaks its rules: {e}') from e
    return metadata


def _index(source: litcache_pb2.MetadataSource) -> upstream_record.Index:
    if source.HasField('pubmed'):
        return upstream_record.Index.PUBMED
    return upstream_record.Index.CROSSREF if source.HasField('crossref_json') else upstream_record.Index.OPENALEX


def read(source: litcache_pb2.MetadataSource) -> litcache_pb2.Metadata:
    """Read the paper's `Metadata` from the records the source record holds, unchecked (see `fill`).

    Every field comes from the resolving index's record, PubMed's or Crossref's, or OpenAlex's where it is the only one;
    where OpenAlex's record sits beside another and names at least one author, the authors come from it instead.

    Raises:
        upstream_record.SchemaDriftError: If a field a reader reads has a shape it cannot read.
        ValueError: As `fill`.
    """
    if source.HasField('pubmed'):
        metadata = read_pubmed(source.pubmed)
    elif source.HasField('crossref_json'):
        metadata = read_crossref(upstream_record.parse_text(source.crossref_json))
    elif source.HasField('openalex_json'):
        return read_openalex(upstream_record.parse_text(source.openalex_json))
    else:
        raise ValueError('the source record holds no index record to fill the metadata from')
    if source.HasField('openalex_json'):
        root = upstream_record.JsonObject.root(
            upstream_record.Index.OPENALEX, upstream_record.parse_text(source.openalex_json)
        )
        if authors := _openalex_authors(root):
            del metadata.authors[:]
            metadata.authors.extend(authors)
    return metadata


def read_pubmed(record: litcache_pb2.PubmedRecord) -> litcache_pb2.Metadata:
    """Read the metadata from PubMed's typed record, a journal article's or a book's."""
    match record.WhichOneof('kind'):
        case 'article':
            citation = record.article.medline_citation
            article = citation.article
            issue = article.journal.journal_issue
            return litcache_pb2.Metadata(
                title=_pubmed_text(article.article_title.value),
                authors=_pubmed_authors([article.author_list] if article.HasField('author_list') else []),
                year=_pubmed_year(issue.pub_date) if issue.HasField('pub_date') else '',
                container_title=_collapse(article.journal.title),
                abstract=_pubmed_abstract(article.abstract),
                mesh_headings=_pubmed_mesh_headings(citation.mesh_heading_list),
                publication_types=_pubmed_publication_types(article.publication_type_list),
            )
        case 'book_article':
            return _read_pubmed_book(record.book_article.book_document)
        case kind:
            raise ValueError(f'PubmedRecord kind {kind!r} has no metadata reader')


def _read_pubmed_book(document: pubmed_pb2.BookDocument) -> litcache_pb2.Metadata:
    book = document.book
    book_year = _pubmed_year(book.pub_date) if book.HasField('pub_date') else ''
    if not document.article_title.value:  # a whole book
        return litcache_pb2.Metadata(
            title=_pubmed_text(book.book_title.value),
            authors=_pubmed_authors(document.author_list) or _pubmed_authors(book.author_list),
            year=book_year,
            container_title='',
            abstract=_pubmed_abstract(document.abstract),
            publication_types=_pubmed_publication_types(document.publication_type),
        )
    # A chapter: its authors are its own list's, or the book's authors where it names none (a monograph's chapters
    # share its authors); its year is when it was contributed, which for a GeneReviews chapter is not the book's.
    contributed = _pubmed_year(document.contribution_date) if document.HasField('contribution_date') else ''
    return litcache_pb2.Metadata(
        title=_pubmed_text(document.article_title.value),
        authors=_pubmed_authors(document.author_list) or _pubmed_authors(book.author_list),
        year=contributed or book_year,
        container_title=_pubmed_text(book.book_title.value),
        abstract=_pubmed_abstract(document.abstract),
        publication_types=_pubmed_publication_types(document.publication_type),
    )


def read_crossref(work: Mapping[str, object]) -> litcache_pb2.Metadata:
    """Read the metadata from a Crossref work (the `message` object of a works response).

    Raises:
        upstream_record.SchemaDriftError: If a field read has a shape this reader cannot read.
    """
    root = upstream_record.JsonObject.root(upstream_record.Index.CROSSREF, work)
    authors: list[litcache_pb2.Metadata.Author] = []
    for author in root.optional_object_list('author') or []:
        given, family, name = (
            _markup_text(author.optional_str_or_null(key) or '') for key in ('given', 'family', 'name')
        )
        orcid = _orcid(author.optional_str_or_null('ORCID') or '')
        if family:
            authors.append(_Author(name=' '.join(part for part in (given, family) if part), orcid=orcid))
        elif name or given:
            authors.append(_Author(name=name or given, orcid=orcid))
    abstract = root.optional_str_or_null('abstract')
    return litcache_pb2.Metadata(
        title=_markup_text(_first(root.optional_str_list('title'))),
        authors=authors,
        year=_crossref_year(root),
        container_title=_markup_text(_first(root.optional_str_list('container-title'))),
        abstract=_markup_text(abstract) if abstract else '',
        publication_types=_crossref_publication_types(root),
    )


def read_openalex(work: Mapping[str, object]) -> litcache_pb2.Metadata:
    """Read the metadata from an OpenAlex work (one element of a works response's `results`).

    Raises:
        upstream_record.SchemaDriftError: If `title` is missing, a `mesh` row lacks `descriptor_ui`, `qualifier_ui` or
            `is_major_topic` or states a null `descriptor_ui` or `is_major_topic`, or a field read has a shape this
            reader cannot read.
    """
    root = upstream_record.JsonObject.root(upstream_record.Index.OPENALEX, work)
    year = root.optional_int_or_null('publication_year')
    if year is not None and not _FIRST_YEAR <= year <= _LAST_YEAR:
        raise root.drift('publication_year', f'is {year}, where a four-digit year is read')
    location = root.optional_object_or_null('primary_location')
    source = location.optional_object_or_null('source') if location is not None else None
    container = source.optional_str_or_null('display_name') if source is not None else None
    return litcache_pb2.Metadata(
        title=_markup_text(root.required_str_or_null('title') or ''),
        authors=_openalex_authors(root),
        year='' if year is None else str(year),
        container_title=_markup_text(container or ''),
        abstract=_LEADING_ABSTRACT.sub('', _collapse(_openalex_abstract(root))),
        mesh_headings=_openalex_mesh_headings(root),
        publication_types=_mapped(_OPENALEX_PUBLICATION_TYPES.get(root.optional_str_or_null('type') or '')),
    )


def _openalex_authors(root: upstream_record.JsonObject) -> list[litcache_pb2.Metadata.Author]:
    authors: list[litcache_pb2.Metadata.Author] = []
    for authorship in root.optional_object_list('authorships') or []:
        author = authorship.optional_object_or_null('author')
        if author is None:
            continue
        name = _markup_text(author.optional_str_or_null('display_name') or '')
        if name:
            authors.append(_Author(name=name, orcid=_orcid(author.optional_str_or_null('orcid') or '')))
    return authors


def _pubmed_authors(lists: Iterable[pubmed_pb2.AuthorList]) -> list[litcache_pb2.Metadata.Author]:
    """The authors in the first list of authors, an editors' list and invalid or nameless entries left out."""
    for author_list in lists:
        if author_list.HasField('type') and author_list.type == _AuthorList.TYPE_EDITORS:
            continue
        valid = (a for a in author_list.author if not (a.HasField('valid_yn') and not a.valid_yn))
        named = ((_pubmed_name(a), a) for a in valid)
        return [_Author(name=name, orcid=_pubmed_orcid(author)) for name, author in named if name]
    return []


def _pubmed_name(author: pubmed_pb2.Author) -> str:
    if author.HasField('collective_name'):
        return _pubmed_text(author.collective_name.value)
    given = author.fore_name if author.HasField('fore_name') else author.initials
    return _collapse(' '.join(part for part in (given, author.last_name) if part))


def _pubmed_mesh_headings(headings: Iterable[pubmed_pb2.MeshHeading]) -> list[litcache_pb2.Metadata.MeshHeading]:
    return [
        _MeshHeading(
            descriptor=_MeshTerm(ui=heading.descriptor_name.ui, major_topic=heading.descriptor_name.major_topic_yn),
            qualifiers=[_MeshTerm(ui=q.ui, major_topic=q.major_topic_yn) for q in heading.qualifier_name],
        )
        for heading in headings
    ]


def _pubmed_publication_types(types: Iterable[pubmed_pb2.PublicationType]) -> list[str]:
    """The types' names in record order, each once; a name stating nothing is left out."""
    names = (_collapse(publication_type.value) for publication_type in types)
    return list(dict.fromkeys(name for name in names if name))


def _pubmed_orcid(author: pubmed_pb2.Author) -> str:
    """The first well-formed ORCID iD among the author's identifiers, in its bare form; "" where none is."""
    stated = (_orcid(i.value) for i in author.identifier if i.source == _ORCID_SOURCE)
    return next((orcid for orcid in stated if orcid), '')


def _orcid(stated: str) -> str:
    """The bare form of an ORCID iD a record states; "" where it states none, or one malformed or failing its check.

    The check digit is ISO 7064 MOD 11-2's over the iD's 15 other digits, `X` standing for 10.
    """
    match = _STATED_ORCID.fullmatch(stated.strip())
    if match is None:
        return ''
    orcid = match[1].upper()
    total = 0
    for digit in orcid[:-1].replace('-', ''):
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return orcid if orcid[-1] == ('X' if check == 10 else str(check)) else ''


def _pubmed_year(date: timestamp_pb2.Timestamp) -> str:
    year = date.ToDatetime().year
    return str(year) if year != _UNPARSED_YEAR and _FIRST_YEAR <= year <= _LAST_YEAR else ''


def _pubmed_abstract(abstract: pubmed_pb2.Abstract) -> str:
    sections = (
        f'{_collapse(text.label)}: {_pubmed_text(text.value)}' if text.label else _pubmed_text(text.value)
        for text in abstract.abstract_text
    )
    return _collapse(' '.join(sections))


def _crossref_publication_types(root: upstream_record.JsonObject) -> list[str]:
    work_type, subtype = root.optional_str_or_null('type'), root.optional_str_or_null('subtype')
    table = _CROSSREF_PUBLICATION_TYPES
    return _mapped(table.get((work_type or '', subtype)) or table.get((work_type or '', None)))


def _mapped(publication_type: str | None) -> list[str]:
    return [] if publication_type is None else [publication_type]


def _crossref_year(root: upstream_record.JsonObject) -> str:
    issued = root.optional_object_or_null('issued')
    if issued is None:
        return ''
    parts = issued.optional_list('date-parts')
    if not parts:
        return ''
    first = parts[0]
    if not isinstance(first, list):
        raise issued.drift('date-parts[0]', f'is {upstream_record.json_type(first)}, where an array is read')
    if not first or first[0] is None:
        return ''
    year = first[0]
    if isinstance(year, bool) or not isinstance(year, int):
        raise issued.drift('date-parts[0][0]', f'is {upstream_record.json_type(year)}, where an integer is read')
    if not _FIRST_YEAR <= year <= _LAST_YEAR:
        raise issued.drift('date-parts[0][0]', f'is {year}, where a four-digit year is read')
    return str(year)


def _openalex_mesh_headings(root: upstream_record.JsonObject) -> list[litcache_pb2.Metadata.MeshHeading]:
    """The headings OpenAlex's `mesh` rows (one per descriptor and qualifier, often repeated) flatten.

    Headings and their qualifiers are in first-seen order. A heading's descriptor is a major topic if any of its rows is
    marked one, and its qualifiers never are: OpenAlex marks every row of a major heading alike.
    """
    headings: dict[str, litcache_pb2.Metadata.MeshHeading] = {}
    for row in root.optional_object_list_or_null('mesh') or []:
        descriptor = row.required_str('descriptor_ui')
        qualifier = row.required_str_or_null('qualifier_ui')
        major_topic = row.required_bool('is_major_topic')
        heading = headings.setdefault(descriptor, _MeshHeading(descriptor=_MeshTerm(ui=descriptor)))
        heading.descriptor.major_topic |= major_topic
        if qualifier is not None and all(q.ui != qualifier for q in heading.qualifiers):
            heading.qualifiers.add(ui=qualifier)
    return list(headings.values())


def _openalex_abstract(root: upstream_record.JsonObject) -> str:
    """The abstract an inverted index (word → its positions) encodes, its words put back in position order."""
    index = root.optional_object_or_null('abstract_inverted_index')
    if index is None:
        return ''
    placed: list[tuple[int, str]] = []
    for word in index.checked_keys():
        positions = index.optional_list(word) or []
        for position in positions:
            if isinstance(position, bool) or not isinstance(position, int):
                raise index.drift(word, f'holds {upstream_record.json_type(position)}, where a position is read')
            placed.append((position, word))
    return ' '.join(word for _, word in sorted(placed))


class _PlainText(html.parser.HTMLParser):
    """The text of JATS or HTML markup, a section title kept as the `Label: ` it heads.

    It is fed only markup tags, every other `<` escaped, no CDATA section and no element it reads as raw text
    (`_escape_text`). So what the parser makes of a stray `<` or of raw text, which differs between Python patch
    releases, never decides what text is kept.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._title: list[str] | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.rpartition(':')[2]
        if name == 'title':
            if self._title is None:  # a title nested in one is part of it
                self._title = []
        elif name in _BLOCK_ELEMENTS:
            self._text(' ')

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.rpartition(':')[2] in _BLOCK_ELEMENTS:
            self._text(' ')

    @override
    def handle_endtag(self, tag: str) -> None:
        name = tag.rpartition(':')[2]
        if name == 'title' and self._title is not None:
            label = _collapse(''.join(self._title)).rstrip(':').rstrip()
            self._title = None
            # A title that only says "Abstract" heads the whole abstract, which needs no label.
            self.parts.append(' ' if not label or label.casefold() == 'abstract' else f' {label}: ')
        elif name in _BLOCK_ELEMENTS:
            self._text(' ')

    @override
    def handle_data(self, data: str) -> None:
        self._text(data)

    @override
    def close(self) -> None:
        super().close()
        if self._title is not None:  # a title never closed is text, not a label
            self.parts.extend((' ', *self._title))
            self._title = None

    def _text(self, text: str) -> None:
        (self._title if self._title is not None else self.parts).append(text)


def _is_markup(tag: re.Match[str]) -> bool:
    prefix, name = tag['prefix'], tag['name'].lower()
    if prefix is not None and _MARKUP_PREFIX.fullmatch(prefix.lower()):
        return True
    return name in _MARKUP_ELEMENTS or name in _MATHML_ELEMENTS


def _escape_text(text: str, escape_ampersands: Callable[[str], str]) -> str:
    """Prepare text for `_PlainText`: every `<` that opens no markup tag escaped, and each CDATA section made text.

    `escape_ampersands` is applied to everything outside a CDATA section, whose content is literal.
    """
    out: list[str] = []
    pos = 0
    cdata_end = -2  # where the last `]]>` found starts: -2 before any search, -1 once none is left
    while (lt := text.find('<', pos)) != -1:
        out.append(escape_ampersands(text[pos:lt]))
        if text.startswith(_CDATA_START, lt) and cdata_end != -1:
            if cdata_end < lt:
                cdata_end = text.find(_CDATA_END, lt)
            if cdata_end != -1:
                out.append(html.escape(text[lt + len(_CDATA_START) : cdata_end], quote=False))
                pos = cdata_end + len(_CDATA_END)
                continue
        tag = _TAG.match(text, lt)
        if tag is not None and _is_markup(tag):
            pos = tag.end()
            raw = tag[0]
            if tag['prefix'] is None and tag['name'].lower() in _RAW_TEXT_ELEMENTS:
                raw = raw.replace(tag['name'], f'x:{tag["name"]}', 1)
            out.append(escape_ampersands(raw))
        else:
            pos = lt + 1
            out.append('&lt;')
    out.append(escape_ampersands(text[pos:]))
    return ''.join(out)


def _markup_text(text: str) -> str:
    """Plain text from a string that may carry JATS or HTML markup (Crossref's and OpenAlex's)."""
    decoded = _strip_markup(_escape_text(text, lambda chunk: _UNTERMINATED_AMPERSAND.sub('&amp;', chunk)))
    # Markup escaped twice upstream is a tag now; every `&` escaped, the second pass decodes nothing.
    return _collapse(_strip_markup(_escape_text(decoded, lambda chunk: chunk.replace('&', '&amp;'))))


def _strip_markup(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    parser.close()
    return ''.join(parser.parts)


def _pubmed_text(text: str) -> str:
    """Plain text from the PubMed converter's markdown, through the converter's own inverse."""
    return _collapse(xml_converter.markdown_to_plain_text(text))


def _collapse(text: str) -> str:
    return _SPACE.sub(' ', text).strip()


def _first(values: list[str] | None) -> str:
    return next((value for value in values or [] if value), '')
