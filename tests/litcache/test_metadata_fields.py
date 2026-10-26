"""The metadata read from each index's record, as plain text, and the fill that makes a `Metadata` of it."""

from __future__ import annotations

import json
import pathlib

import pytest
from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.litcache import efetch, metadata_fields, openalex, upstream_record
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).parent.parent / 'fixtures' / 'litcache'
_EMPTY = litcache_pb2.Metadata()


def _names(metadata: litcache_pb2.Metadata) -> list[str]:
    return [author.name for author in metadata.authors]


def _openalex_work(path: str) -> dict[str, object]:
    return json.loads((_FIXTURES / path).read_text('utf-8'))['results'][0]


def _crossref_work(path: str) -> dict[str, object]:
    return json.loads((_FIXTURES / path).read_text('utf-8'))['message']


def _pubmed_article() -> litcache_pb2.PubmedRecord:
    parsed = efetch.parse_set((_FIXTURES / 'oa' / 'efetch.xml').read_bytes())
    return litcache_pb2.PubmedRecord(article=next(iter(parsed.articles.values())))


def _pubmed_chapter() -> litcache_pb2.PubmedRecord:
    parsed = efetch.parse_set((_FIXTURES / 'bookshelf' / 'efetch.xml').read_bytes())
    return litcache_pb2.PubmedRecord(book_article=next(iter(parsed.book_articles.values())))


def _stamp(date: str) -> timestamp_pb2.Timestamp:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromJsonString(f'{date}T00:00:00Z')
    return stamp


def test_a_pubmed_article_reads_its_fields() -> None:
    fields = metadata_fields.read_pubmed(_pubmed_article())
    assert fields.title.startswith('Whole exome sequencing in 342')
    assert _names(fields)[:2] == ['Alexander H Li', 'Neil A Hanchard']
    assert (fields.year, fields.container_title) == ('2017', 'Genome medicine')
    assert fields.abstract.startswith('BACKGROUND: Left-sided lesions')


def test_a_pubmed_chapter_reads_its_book_as_its_container() -> None:
    fields = metadata_fields.read_pubmed(_pubmed_chapter())
    assert (fields.title, _names(fields), fields.container_title) == (
        'A synthetic chapter',
        ['Jane Doe'],
        'A synthetic review series',
    )


def _genereviews_chapter() -> pubmed_pb2.PubmedBookArticle:
    """A GeneReviews chapter as efetch states one (PMID 20301428's shape, its text invented)."""
    book = pubmed_pb2.PubmedBookArticle()
    document = book.book_document
    document.book.book_title.value = 'GeneReviews^(®)'
    document.book.pub_date.CopyFrom(_stamp('1993-01-01'))
    editors = document.book.author_list.add(type=pubmed_pb2.AuthorList.TYPE_EDITORS)
    editors.author.add(fore_name='Margaret P', last_name='Adam', valid_yn=True)
    document.article_title.value = 'A Condition'
    document.contribution_date.CopyFrom(_stamp('2001-03-26'))
    document.author_list.add(type=pubmed_pb2.AuthorList.TYPE_AUTHORS).author.add(
        fore_name='Adrienne', last_name='Savant', valid_yn=True
    )
    abstract = document.abstract.abstract_text
    abstract.add(label='CLINICAL CHARACTERISTICS', value='A condition of the *CFTR* gene.')
    abstract.add(label='MANAGEMENT', value='*Targeted therapies:* modulators.')
    return book


def test_a_genereviews_chapter_is_dated_by_its_contribution_and_titled_in_plain_text() -> None:
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(book_article=_genereviews_chapter()))
    assert fields == litcache_pb2.Metadata(
        title='A Condition',
        authors=[litcache_pb2.Metadata.Author(name='Adrienne Savant')],  # the book's editors are not its authors
        year='2001',  # contributed in 2001, not the book's 1993
        container_title='GeneReviews®',
        abstract='CLINICAL CHARACTERISTICS: A condition of the CFTR gene. MANAGEMENT: Targeted therapies: modulators.',
    )


def test_a_chapter_with_no_contribution_date_is_dated_by_its_book() -> None:
    chapter = _genereviews_chapter()
    chapter.book_document.ClearField('contribution_date')
    assert metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(book_article=chapter)).year == '1993'


def test_a_chapter_naming_no_authors_takes_its_books_authors() -> None:
    chapter = _genereviews_chapter()
    chapter.book_document.ClearField('author_list')
    chapter.book_document.book.author_list.add(type=pubmed_pb2.AuthorList.TYPE_AUTHORS).author.add(
        fore_name='Mono', last_name='Graph'
    )
    assert _names(metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(book_article=chapter))) == ['Mono Graph']


def test_a_whole_book_has_no_container_and_its_own_authors() -> None:
    book = pubmed_pb2.PubmedBookArticle()
    book.book_document.book.book_title.value = 'A book'
    book.book_document.book.author_list.add(type=pubmed_pb2.AuthorList.TYPE_EDITORS).author.add(
        fore_name='Ed', last_name='Itor'
    )
    book.book_document.book.author_list.add(type=pubmed_pb2.AuthorList.TYPE_AUTHORS).author.add(
        fore_name='Au', last_name='Thor'
    )
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(book_article=book))
    assert (fields.title, _names(fields), fields.container_title) == ('A book', ['Au Thor'], '')


def test_pubmed_authors_keep_collective_names_and_drop_invalid_or_nameless_entries() -> None:
    record = pubmed_pb2.PubmedArticle()
    authors = record.medline_citation.article.author_list.author
    authors.add(fore_name='Jane', last_name='Doe')
    authors.add().collective_name.value = 'The Consortium'
    authors.add(fore_name='Wrong', last_name='Entry', valid_yn=False)
    authors.add(initials='JQ', last_name='Public')
    authors.add()  # states no name
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    assert _names(fields) == ['Jane Doe', 'The Consortium', 'JQ Public']


@pytest.mark.parametrize(('stamp', 'expected'), [(None, ''), ('0001-01-01', ''), ('2019-01-01', '2019')])
def test_a_pubmed_year_is_empty_when_none_parses(stamp: str | None, expected: str) -> None:
    record = pubmed_pb2.PubmedArticle()
    if stamp is not None:
        record.medline_citation.article.journal.journal_issue.pub_date.CopyFrom(_stamp(stamp))
    assert metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record)).year == expected


def test_pubmed_markdown_is_read_as_plain_text() -> None:
    record = pubmed_pb2.PubmedArticle()
    article = record.medline_citation.article
    article.article_title.value = 'Loss of *BRCA1*  in **H~(2)O**\n and the __m^(2)__ lesion'
    article.abstract.abstract_text.add(label='RESULTS', value='*BRCA1* m^(2)')
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    assert fields.title == 'Loss of BRCA1 in H2O and the m2 lesion'
    assert fields.abstract == 'RESULTS: BRCA1 m2'


def test_pubmed_fields_the_converter_copies_as_they_are_only_collapse_whitespace() -> None:
    # The converter renders no markdown into a journal title, an author's name parts or a section label, so a `_` or
    # a `*` there is the record's own character, not a marker.
    record = pubmed_pb2.PubmedArticle()
    article = record.medline_citation.article
    article.journal.title = 'J_Med  *Genet*'
    article.author_list.author.add(fore_name='Jo_Ann', last_name='Smith*')
    article.abstract.abstract_text.add(label='AIM_1', value='Text.')
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    assert (fields.container_title, _names(fields), fields.abstract) == (
        'J_Med *Genet*',
        ['Jo_Ann Smith*'],
        'AIM_1: Text.',
    )


@pytest.mark.parametrize('kind', ['article', 'chapter'])
def test_a_pubmed_title_reads_back_a_marker_it_states_literally(kind: str) -> None:
    # The converter writes italics as `*…*` and escapes a literal `*`, here a stop codon.
    stated = r'Stop codon (p.R391\*) in *SERPINC1*'
    if kind == 'article':
        record = litcache_pb2.PubmedRecord(article=pubmed_pb2.PubmedArticle())
        record.article.medline_citation.article.article_title.value = stated
    else:
        record = litcache_pb2.PubmedRecord(book_article=pubmed_pb2.PubmedBookArticle())
        record.book_article.book_document.article_title.value = stated
    assert metadata_fields.read_pubmed(record).title == 'Stop codon (p.R391*) in SERPINC1'


def test_a_crossref_work_reads_its_fields() -> None:
    fields = metadata_fields.read_crossref(_crossref_work('oa/crossref.json'))
    assert fields.title.startswith('Whole exome sequencing in 342')
    assert _names(fields)[:2] == ['Alexander H. Li', 'Neil A. Hanchard']
    assert (fields.year, fields.container_title, fields.abstract) == ('2017', 'Genome Medicine', '')


def test_a_crossref_jats_abstract_is_read_as_plain_text_without_its_heading() -> None:
    fields = metadata_fields.read_crossref(_crossref_work('preprint/crossref.json'))
    assert fields.abstract.startswith('Interactions between plants and soil microbes play an important role')
    assert '<' not in fields.abstract
    assert '  ' not in fields.abstract


def test_crossref_markup_and_section_titles_are_read_as_plain_text() -> None:
    work = {
        'title': ['The <i>BRCA1</i> &amp; <i>BRCA2</i>\n   genes'],
        'abstract': (
            '<jats:sec><jats:title>Background</jats:title><jats:p>Variants in <jats:italic>BRCA1</jats:italic>'
            '</jats:p></jats:sec><jats:sec><jats:title>Methods</jats:title><jats:p>We sequenced.</jats:p></jats:sec>'
        ),
    }
    fields = metadata_fields.read_crossref(work)
    assert fields.title == 'The BRCA1 & BRCA2 genes'
    assert fields.abstract == 'Background: Variants in BRCA1 Methods: We sequenced.'


def test_crossref_authors_drop_an_entry_stating_no_name_and_keep_a_given_name_alone() -> None:
    # The live shape of a nameless entry (10.58837/chula.the.1990.15): sequence and empty lists, no name.
    work = {
        'author': [
            {'name': 'The Consortium'},
            {'sequence': 'additional', 'affiliation': [], 'role': [{'role': 'author'}]},
            {'family': 'Doe', 'given': None},
            {'given': 'Jane', 'family': 'Roe'},
            {'given': 'Only', 'family': ''},
        ]
    }
    assert _names(metadata_fields.read_crossref(work)) == ['The Consortium', 'Doe', 'Jane Roe', 'Only']


@pytest.mark.parametrize(
    ('jats', 'expected'),
    [
        ('<jats:p>MAF &lt; 0.01 in c.1A&gt;G carriers</jats:p>', 'MAF < 0.01 in c.1A>G carriers'),
        ('<jats:p>MAF < 0.01 … c.1A>G</jats:p>', 'MAF < 0.01 … c.1A>G'),
        ('p < 0.05 and q > 0.1', 'p < 0.05 and q > 0.1'),
        ('n<5 vs n>10', 'n<5 vs n>10'),
        ('A &amp;lt; B', 'A &lt; B'),  # decoded once
        ('<jats:p>one</jats:p><jats:p>two<jats:break/>three</jats:p>', 'one two three'),
        ('<jats:title>Background:</jats:title><jats:p>Text.</jats:p>', 'Background: Text.'),
        ('<jats:title>Abstract:</jats:title><jats:p>Text.</jats:p>', 'Text.'),
        ('<jats:title>P &lt; 0.05 cohorts</jats:title><jats:p>Text.</jats:p>', 'P < 0.05 cohorts: Text.'),
        ('<jats:p>Abstract reasoning is tested.</jats:p>', 'Abstract reasoning is tested.'),
    ],
)
def test_a_crossref_abstract_is_read_as_plain_text(jats: str, expected: str) -> None:
    assert metadata_fields.read_crossref({'abstract': jats}).abstract == expected


@pytest.mark.parametrize(
    ('title', 'expected'),
    [
        # Live shapes: 10.1111/bjh.13142, 10.1142/9789814313933_0008, 10.1017/ash.2022.213.
        ('<scp>MRD</scp> in <scp>AML</scp>', 'MRD in AML'),
        ('<font>HD</font> and <font face="Arial">PD</font>', 'HD and PD'),
        ('Cost<jats:table-wrap id="t1" position="float"><jats:table><jats:tr><jats:td>$1</jats:td>', 'Cost $1'),
        ('<jats:inline-graphic xlink:href="x.gif"/>Stem cells', 'Stem cells'),
        # Escaped twice upstream (10.48130/opr-0024-0028): a recognised tag once decoded is stripped too.
        ('Roots of &lt;i&gt;Arabidopsis&lt;/i&gt; thaliana', 'Roots of Arabidopsis thaliana'),
        ('a &lt; b and c &gt; d', 'a < b and c > d'),
        ('&lt;x&gt; carriers', '<x> carriers'),
        # Only a reference closed by `;` is decoded, as in XML.
        ('&parameter &para; R&D', '&parameter ¶ R&D'),
        ('<![CDATA[x < y]]> holds', 'x < y holds'),
        ('<jats:title>Background', 'Background'),
        # Text that only looks like the start of a tag stays, wherever it falls.
        ('<jats:p>When a&lt;b, the rest follows.</jats:p>', 'When a<b, the rest follows.'),
        ('MAF &lt;x', 'MAF <x'),
        ('a<b c', 'a<b c'),
        ('a </3 b', 'a </3 b'),
        ('</CD4> T cells', '</CD4> T cells'),
        ('a <!-- b', 'a <!-- b'),
        # Live shapes: any namespace prefix (10.5040/9798216404231, 10.1155/2022/9014702), and void and rarer elements.
        ('<JATS1:p>Text</jats1:p>', 'Text'),
        ('<a:math xmlns:a="http://www.w3.org/1998/Math/MathML"><a:mi>x</a:mi></a:math> holds', 'x holds'),
        ('<smlcap>Ab</smlcap> <disp-formula content-type="math/mathml">y</disp-formula>', 'Ab y'),
        ('<img src="/c.php" alt="Hit counter" /> Text', 'Text'),
        ('<f:mi>x</f:mi> and <Na:K> ratio', 'x and <Na:K> ratio'),
        ('V<inf>0</inf> drift', 'V0 drift'),
        (
            '“THE GREY ZONE”<subtitle>The Problem</subtitle>',
            '“THE GREY ZONE” The Problem',
        ),  # 10.1093/oxfordjournals.pa.a054058  # IEEE's subscript (10.1109/icce-berlin.2016.7684763)
        ('<a href="u"title="t">Text</a>', 'Text'),
        ('<![CDATA[R&D]]> costs', 'R&D costs'),
        ('<![CDATA[a] ]>b]]> c', 'a] ]>b c'),
        ('<m:mmultiscripts><m:mi>x</m:mi><m:none/><m:mi>y</m:mi></m:mmultiscripts>', 'xy'),
        # A title's words stay apart, and a title nested in one is part of it, on every Python the parser differs on.
        ('<title>A<br/>B</title>x', 'A B: x'),
        ('<jats:title>A<jats:break/>B</jats:title>x', 'A B: x'),
        ('<title>a<title>b</title>c</title>d', 'ab: cd'),
    ],
)
def test_crossref_markup_is_stripped_to_plain_text(title: str, expected: str) -> None:
    assert metadata_fields.read_crossref({'title': [title]}).title == expected


@pytest.mark.parametrize(
    ('author', 'path'), [({'family': ['Doe']}, r'author\[0\]\.family'), ({'name': 5}, r'author\[0\]\.name')]
)
def test_a_crossref_author_name_of_the_wrong_type_drifts(author: dict[str, object], path: str) -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match=path):
        metadata_fields.read_crossref({'author': [author]})


@pytest.mark.parametrize('issued', [None, {'date-parts': [[None]]}, {'date-parts': []}])
def test_a_crossref_work_with_no_year_reads_empty(issued: dict[str, object] | None) -> None:
    work: dict[str, object] = {} if issued is None else {'issued': issued}
    assert metadata_fields.read_crossref(work) == _EMPTY


def test_a_crossref_year_that_is_not_an_integer_drifts() -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match=r'issued\.date-parts\[0\]\[0\]'):
        metadata_fields.read_crossref({'issued': {'date-parts': [['2017']]}})


@pytest.mark.parametrize('path', ['oa/openalex.json', 'preprint/openalex.json'])
def test_an_openalex_work_reads_its_fields(path: str) -> None:
    work = _openalex_work(path)
    fields = metadata_fields.read_openalex(work)
    assert fields.title == work['title']
    assert fields.year == str(work['publication_year'])
    assert len(fields.authors) == len(work['authorships'])  # type: ignore[arg-type]
    assert fields.container_title
    assert fields.abstract
    assert not fields.abstract.startswith('Abstract ')


def test_an_openalex_abstract_is_plain_text_and_never_read_as_markup() -> None:
    work = {'title': 'T', 'abstract_inverted_index': {'MAF': [0], '<': [1], '0.01;': [2], 'c.1A>G': [3], '&amp;': [4]}}
    assert metadata_fields.read_openalex(work).abstract == 'MAF < 0.01; c.1A>G &amp;'


def test_an_openalex_abstract_word_with_a_lone_surrogate_drifts() -> None:
    work = {'title': 'T', 'abstract_inverted_index': {'ok': [0], 'bad\ud800': [1]}}
    with pytest.raises(upstream_record.SchemaDriftError, match='lone surrogate'):
        metadata_fields.read_openalex(work)


def test_an_openalex_abstract_is_rebuilt_in_position_order() -> None:
    work = {'title': 'T', 'abstract_inverted_index': {'Abstract': [0], 'the': [2, 5], 'Beat': [1], 'gene': [3, 6]}}
    work['abstract_inverted_index']['and'] = [4]  # type: ignore[index]
    assert metadata_fields.read_openalex(work).abstract == 'Beat the gene and the gene'


def test_prose_that_starts_with_the_word_abstract_keeps_it() -> None:
    work = {'title': 'T', 'abstract_inverted_index': {'Abstract': [0], 'reasoning': [1], 'helps': [2]}}
    assert metadata_fields.read_openalex(work).abstract == 'Abstract reasoning helps'


def test_an_openalex_work_with_nothing_stated_reads_empty() -> None:
    work = {
        'title': None,
        'publication_year': None,
        'primary_location': {'source': None},
        'authorships': [{'author': None}, {'author': {'display_name': None}}, {'author': {'display_name': ''}}],
        'abstract_inverted_index': None,
    }
    assert metadata_fields.read_openalex(work) == _EMPTY


def test_openalex_html_is_read_as_plain_text() -> None:
    work = {
        'title': 'The <i>BRCA1</i> &amp; <sub>2</sub> gene',
        'authorships': [{'author': {'display_name': 'Zoë  Ng'}}],
    }
    fields = metadata_fields.read_openalex(work)
    assert (fields.title, _names(fields)) == ('The BRCA1 & 2 gene', ['Zoë Ng'])


@pytest.mark.parametrize(
    ('work', 'path'),
    [
        ({'authorships': []}, 'title is missing'),
        (
            {'title': 'T', 'authorships': [{'author': {'display_name': 5}}]},
            r'authorships\[0\]\.author\.display_name',
        ),
        ({'title': 'T', 'authorships': ['Jane Doe']}, r'authorships\[0\] is a string'),
        ({'title': 'T', 'abstract_inverted_index': {'gene': ['one']}}, r'abstract_inverted_index\.gene'),
    ],
)
def test_an_openalex_field_in_an_unreadable_shape_drifts(work: dict[str, object], path: str) -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match=path):
        metadata_fields.read_openalex(work)


@pytest.mark.parametrize(
    'source',
    [
        litcache_pb2.MetadataSource(pubmed=_pubmed_article()),
        litcache_pb2.MetadataSource(crossref_json=upstream_record.to_text(_crossref_work('oa/crossref.json'))),
        litcache_pb2.MetadataSource(openalex_json=upstream_record.to_text(_openalex_work('oa/openalex.json'))),
    ],
    ids=['pubmed', 'crossref', 'openalex'],
)
def test_fill_reads_every_field_from_the_one_record(source: litcache_pb2.MetadataSource) -> None:
    metadata = metadata_fields.fill(source)
    assert metadata.title.startswith('Whole exome sequencing in 342')
    assert (metadata.year, bool(metadata.authors), bool(metadata.container_title)) == ('2017', True, True)


def test_fill_refuses_a_source_record_with_no_index_record() -> None:
    with pytest.raises(ValueError, match='no index record'):
        metadata_fields.fill(litcache_pb2.MetadataSource())


def test_an_openalex_record_is_written_with_its_metadata() -> None:
    sourced = openalex.sourced(openalex.parse_work(_openalex_work('preprint/openalex.json')))
    metadata = sourced.metadata
    assert metadata.title.startswith('Opportunistic partner choice')
    assert (metadata.year, metadata.container_title) == ('2024', 'bioRxiv (Cold Spring Harbor Laboratory)')
    assert metadata.abstract.startswith('Interactions between plants and soil microbes')


@pytest.mark.parametrize(
    'title',
    ['p<0.05 and a<b and c>d', 'MAF<0.01 in <x> carriers', 'n<5 vs n>10'],
)
def test_a_less_than_sign_that_opens_no_known_tag_is_text(title: str) -> None:
    assert metadata_fields.read_crossref({'title': [title]}).title == title


def test_a_known_inline_tag_is_markup() -> None:
    assert (
        metadata_fields.read_openalex({'title': 'Loss of <i>BRCA1</i> in <b>mice</b>'}).title == 'Loss of BRCA1 in mice'
    )


def test_names_empty_once_read_as_plain_text_are_left_out() -> None:
    crossref_work = {
        'author': [{'family': ' '}, {'name': '<i></i>'}, {'family': '<b> </b>', 'given': 'Jo'}, {'family': 'Doe'}]
    }
    assert _names(metadata_fields.read_crossref(crossref_work)) == ['Jo', 'Doe']
    openalex_work = {
        'title': 'T',
        'authorships': [{'author': {'display_name': ' '}}, {'author': {'display_name': 'Jane Doe'}}],
    }
    assert _names(metadata_fields.read_openalex(openalex_work)) == ['Jane Doe']


@pytest.mark.parametrize('year', [20017, 999])
def test_a_crossref_year_that_is_not_four_digits_drifts(year: int) -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match='four-digit year'):
        metadata_fields.read_crossref({'issued': {'date-parts': [[year]]}})


@pytest.mark.parametrize('year', [10000, 999])
def test_an_openalex_year_that_is_not_four_digits_drifts(year: int) -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match='publication_year'):
        metadata_fields.read_openalex({'title': 'T', 'publication_year': year})


def test_fill_refuses_metadata_its_rules_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    # A backstop: were a reader to let through a value the rules of `Metadata` refuse, the fill raises drift for the
    # paper rather than a bare error that would abort a whole batch.
    def read(_source: litcache_pb2.MetadataSource) -> litcache_pb2.Metadata:
        return litcache_pb2.Metadata(title='T', authors=[litcache_pb2.Metadata.Author(name='')])

    monkeypatch.setattr(metadata_fields, 'read', read)
    source = litcache_pb2.MetadataSource(openalex_json='{"title": "T"}')
    with pytest.raises(upstream_record.SchemaDriftError, match='breaks its rules') as excinfo:
        metadata_fields.fill(source)
    assert excinfo.value.index == 'openalex'


@pytest.mark.parametrize(
    ('stated', 'expected'),
    [
        ('0000-0002-1825-0097', '0000-0002-1825-0097'),
        ('0000-0002-1694-233X', '0000-0002-1694-233X'),
        ('https://orcid.org/0000-0002-1694-233x', '0000-0002-1694-233X'),
        ('http://orcid.org/0000-0002-1825-0097', '0000-0002-1825-0097'),
        (' https://orcid.org/0000-0002-1825-0097\n', '0000-0002-1825-0097'),
        ('0000-0002-1825-0098', ''),  # the check digit fails
        ('0000-0002-1694-2330', ''),  # the check digit is X
        ('0000000218250097', ''),
        ('https://example.org/0000-0002-1825-0097', ''),
        ('https://orcid.org/0000-0002-1825-0097/', ''),
        ('', ''),
    ],
)
def test_an_orcid_id_is_held_bare_and_one_failing_its_check_is_none(stated: str, expected: str) -> None:
    work = {'author': [{'given': 'Jane', 'family': 'Doe', 'ORCID': stated}]}
    assert metadata_fields.read_crossref(work).authors[0].orcid == expected


def _pubmed_author_with(*identifiers: tuple[str, str]) -> pubmed_pb2.Author:
    author = pubmed_pb2.Author(fore_name='Jane', last_name='Doe')
    for source, value in identifiers:
        author.identifier.add(source=source, value=value)
    return author


def test_a_pubmed_author_takes_the_first_well_formed_orcid_identifier() -> None:
    record = pubmed_pb2.PubmedArticle()
    authors = record.medline_citation.article.author_list.author
    authors.add().CopyFrom(
        _pubmed_author_with(
            ('ISNI', '0000000121032683'), ('ORCID', '0000-0002-1825-0098'), ('ORCID', '0000-0002-1694-233X')
        )
    )
    authors.add().CopyFrom(_pubmed_author_with(('ISNI', '0000000121032683')))
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    assert [author.orcid for author in fields.authors] == ['0000-0002-1694-233X', '']


def test_a_pubmed_chapter_author_carries_their_orcid() -> None:
    chapter = _genereviews_chapter()
    author = chapter.book_document.author_list[0].author[0]
    author.identifier.add(source='ORCID', value='https://orcid.org/0000-0002-1825-0097')
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(book_article=chapter))
    assert [(a.name, a.orcid) for a in fields.authors] == [('Adrienne Savant', '0000-0002-1825-0097')]


def test_an_orcid_stays_with_its_author_when_a_nameless_entry_is_left_out() -> None:
    record = pubmed_pb2.PubmedArticle()
    authors = record.medline_citation.article.author_list.author
    authors.add().identifier.add(source='ORCID', value='0000-0002-1694-233X')  # states no name
    authors.add().CopyFrom(_pubmed_author_with())
    pubmed = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    crossref = metadata_fields.read_crossref({'author': [{'ORCID': '0000-0002-1694-233X'}, {'family': 'Doe'}]})
    openalex = metadata_fields.read_openalex(
        {
            'title': 'T',
            'authorships': [
                {'author': {'display_name': None, 'orcid': 'https://orcid.org/0000-0002-1694-233X'}},
                {'author': {'display_name': 'Jane Doe', 'orcid': None}},
            ],
        }
    )
    for metadata in (pubmed, crossref, openalex):
        assert [a.orcid for a in metadata.authors] == ['']  # the one named author, who states no iD


def test_an_openalex_author_takes_the_profiles_orcid_not_the_deposited_one() -> None:
    work = {
        'title': 'T',
        'authorships': [
            {
                'author': {'display_name': 'Jane Doe', 'orcid': 'https://orcid.org/0000-0002-1825-0097'},
                'raw_orcid': None,
            },
            {
                'author': {'display_name': 'John Roe', 'orcid': None},
                'raw_orcid': 'https://orcid.org/0000-0002-1694-233X',
            },
        ],
    }
    assert [a.orcid for a in metadata_fields.read_openalex(work).authors] == ['0000-0002-1825-0097', '']


@pytest.mark.parametrize(
    ('index', 'work', 'path'),
    [
        ('crossref', {'author': [{'family': 'Doe', 'ORCID': 5}]}, r'author\[0\]\.ORCID is a number'),
        (
            'openalex',
            {'title': 'T', 'authorships': [{'author': {'display_name': 'D', 'orcid': ['x']}}]},
            r'authorships\[0\]\.author\.orcid is an array',
        ),
    ],
)
def test_an_orcid_of_the_wrong_json_type_drifts(index: str, work: dict[str, object], path: str) -> None:
    reader = metadata_fields.read_crossref if index == 'crossref' else metadata_fields.read_openalex
    with pytest.raises(upstream_record.SchemaDriftError, match=path):
        reader(work)


def test_every_orcid_the_preprint_fixtures_state_reaches_its_author() -> None:
    crossref_work = _crossref_work('preprint/crossref.json')
    openalex_work = _openalex_work('preprint/openalex.json')
    cases = [
        (metadata_fields.read_crossref(crossref_work), [entry.get('ORCID') for entry in crossref_work['author']]),  # type: ignore[union-attr, attr-defined]
        (
            metadata_fields.read_openalex(openalex_work),
            [entry['author'].get('orcid') for entry in openalex_work['authorships']],  # type: ignore[union-attr, index]
        ),
    ]
    for metadata, stated in cases:
        assert any(stated)  # rules out a vacuous pass
        assert [a.orcid for a in metadata.authors] == [
            s.removeprefix('https://orcid.org/') if s else '' for s in stated
        ]


_OPENALEX_WORK = 'preprint/openalex.json'


def _resolving_records() -> list[litcache_pb2.MetadataSource]:
    return [
        litcache_pb2.MetadataSource(pubmed=_pubmed_article()),
        litcache_pb2.MetadataSource(pubmed=_pubmed_chapter()),
        litcache_pb2.MetadataSource(crossref_json=upstream_record.to_text(_crossref_work('preprint/crossref.json'))),
    ]


@pytest.mark.parametrize('resolving', _resolving_records(), ids=['pubmed-article', 'pubmed-chapter', 'crossref'])
def test_the_authors_come_from_openalex_beside_the_resolving_record_and_the_rest_from_that_record(
    resolving: litcache_pb2.MetadataSource,
) -> None:
    work = _openalex_work(_OPENALEX_WORK)
    paired = litcache_pb2.MetadataSource()
    paired.CopyFrom(resolving)
    paired.openalex_json = upstream_record.to_text(work)
    alone = metadata_fields.fill(resolving)
    filled = metadata_fields.fill(paired)
    expected_authors = metadata_fields.read_openalex(work).authors
    assert list(filled.authors) == list(expected_authors)
    assert list(alone.authors) != list(expected_authors)  # rules out a vacuous pass
    del filled.authors[:], alone.authors[:]
    assert filled == alone


def test_the_authors_of_a_source_holding_openalex_alone_are_openalexs() -> None:
    work = _openalex_work(_OPENALEX_WORK)
    filled = metadata_fields.fill(litcache_pb2.MetadataSource(openalex_json=upstream_record.to_text(work)))
    assert filled == metadata_fields.read_openalex(work)
    assert filled.authors


@pytest.mark.parametrize(
    'authorships',
    [pytest.param([], id='none'), pytest.param([{'author': {'display_name': None}}], id='none-named')],
)
def test_an_openalex_work_naming_no_author_leaves_the_resolving_records_authors(
    authorships: list[dict[str, object]],
) -> None:
    resolving = litcache_pb2.MetadataSource(pubmed=_pubmed_article())
    paired = litcache_pb2.MetadataSource(
        pubmed=_pubmed_article(), openalex_json=upstream_record.to_text({'title': 'T', 'authorships': authorships})
    )
    alone = metadata_fields.fill(resolving)
    assert alone.authors  # rules out a vacuous pass
    assert metadata_fields.fill(paired) == alone


def test_openalex_author_drift_beside_another_record_drifts_the_fill() -> None:
    source = litcache_pb2.MetadataSource(
        pubmed=_pubmed_article(),
        openalex_json=upstream_record.to_text({'title': 'T', 'authorships': [{'author': {'display_name': 5}}]}),
    )
    with pytest.raises(upstream_record.SchemaDriftError, match=r'authorships\[0\]\.author\.display_name'):
        metadata_fields.fill(source)


def _term(ui: str, *, major_topic: bool = False) -> litcache_pb2.Metadata.MeshTerm:
    return litcache_pb2.Metadata.MeshTerm(ui=ui, major_topic=major_topic)


def _heading(
    descriptor: litcache_pb2.Metadata.MeshTerm, *qualifiers: litcache_pb2.Metadata.MeshTerm
) -> litcache_pb2.Metadata.MeshHeading:
    return litcache_pb2.Metadata.MeshHeading(descriptor=descriptor, qualifiers=qualifiers)


def test_a_pubmed_article_reads_its_mesh_headings_in_record_order() -> None:
    record = pubmed_pb2.PubmedArticle()
    headings = record.medline_citation.mesh_heading_list
    breast = headings.add()
    breast.descriptor_name.CopyFrom(pubmed_pb2.DescriptorName(ui='D001943', major_topic_yn=False))
    breast.qualifier_name.add(ui='Q000235', major_topic_yn=True)
    breast.qualifier_name.add(ui='Q000000981', major_topic_yn=False)
    headings.add().descriptor_name.CopyFrom(pubmed_pb2.DescriptorName(ui='D006801', major_topic_yn=True))
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    assert list(fields.mesh_headings) == [
        _heading(_term('D001943'), _term('Q000235', major_topic=True), _term('Q000000981')),
        _heading(_term('D006801', major_topic=True)),
    ]


def test_the_pubmed_fixture_keeps_every_heading_it_states() -> None:
    record = _pubmed_article()
    stated = record.article.medline_citation.mesh_heading_list
    assert stated  # rules out a vacuous pass
    metadata = metadata_fields.fill(litcache_pb2.MetadataSource(pubmed=record))
    assert [h.descriptor.ui for h in metadata.mesh_headings] == [h.descriptor_name.ui for h in stated]


def _mesh_row(descriptor: str, qualifier: str | None = None, *, major: bool = False) -> dict[str, object]:
    return {
        'descriptor_ui': descriptor,
        'descriptor_name': f'name of {descriptor}',
        'qualifier_ui': qualifier,
        'qualifier_name': None if qualifier is None else f'name of {qualifier}',
        'is_major_topic': major,
    }


def test_openalex_mesh_rows_are_grouped_back_into_headings() -> None:
    rows = [
        _mesh_row('D001943', 'Q000235'),
        _mesh_row('D006801'),  # a descriptor with no qualifier has a row of its own
        _mesh_row('D001943', 'Q000235'),  # a repeated row
        _mesh_row('D001943', 'Q000473', major=True),  # one major row makes the descriptor major
        _mesh_row('D006801'),
        _mesh_row('D001943', 'Q000235', major=True),
    ]
    fields = metadata_fields.read_openalex({'title': 'T', 'mesh': rows})
    assert list(fields.mesh_headings) == [
        # OpenAlex says the heading is a major topic, not which of its terms: its qualifiers are never marked.
        _heading(_term('D001943', major_topic=True), _term('Q000235'), _term('Q000473')),
        _heading(_term('D006801')),
    ]


def test_the_openalex_fixture_regroups_without_losing_a_descriptor() -> None:
    work = _openalex_work('oa/openalex.json')
    rows = work['mesh']
    assert rows  # rules out a vacuous pass
    stated = list(dict.fromkeys(row['descriptor_ui'] for row in rows))  # type: ignore[union-attr, index]
    metadata = metadata_fields.fill(litcache_pb2.MetadataSource(openalex_json=upstream_record.to_text(work)))
    assert [h.descriptor.ui for h in metadata.mesh_headings] == stated


@pytest.mark.parametrize('work', [{'title': 'T'}, {'title': 'T', 'mesh': None}, {'title': 'T', 'mesh': []}])
def test_an_openalex_work_stating_no_mesh_has_no_headings(work: dict[str, object]) -> None:
    assert not metadata_fields.read_openalex(work).mesh_headings


@pytest.mark.parametrize(
    ('mesh', 'path'),
    [
        ('D001943', r'mesh is a string'),
        (['D001943'], r'mesh\[0\] is a string'),
        ([{**_mesh_row('D001943'), 'descriptor_ui': None}], r'mesh\[0\]\.descriptor_ui is null'),
        ([{**_mesh_row('D001943'), 'qualifier_ui': 235}], r'mesh\[0\]\.qualifier_ui is a number'),
        ([{**_mesh_row('D001943'), 'is_major_topic': 'Y'}], r'mesh\[0\]\.is_major_topic is a string'),
        ([{'descriptor_ui': 'D001943', 'qualifier_ui': None}], r'mesh\[0\]\.is_major_topic is missing'),
    ],
)
def test_an_openalex_mesh_row_of_the_wrong_shape_drifts(mesh: object, path: str) -> None:
    with pytest.raises(upstream_record.SchemaDriftError, match=path):
        metadata_fields.read_openalex({'title': 'T', 'mesh': mesh})


@pytest.mark.parametrize('row', [_mesh_row('Q000235'), _mesh_row('D001943', 'D006801'), _mesh_row('Breast Neoplasms')])
def test_an_openalex_mesh_term_of_the_wrong_kind_or_form_fails_the_fill(row: dict[str, object]) -> None:
    source = litcache_pb2.MetadataSource(openalex_json=upstream_record.to_text({'title': 'T', 'mesh': [row]}))
    with pytest.raises(upstream_record.SchemaDriftError, match='breaks its rules'):
        metadata_fields.fill(source)


def test_pubmed_publication_types_are_kept_in_record_order_each_once() -> None:
    record = pubmed_pb2.PubmedArticle()
    types = record.medline_citation.article.publication_type_list
    for name in ('Case Reports', 'Journal  Article\n', ' ', 'Case Reports', "Research Support, Non-U.S. Gov't"):
        types.add(ui='D000000', value=name)
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(article=record))
    assert list(fields.publication_types) == ['Case Reports', 'Journal Article', "Research Support, Non-U.S. Gov't"]


@pytest.mark.parametrize('whole_book', [False, True])
def test_a_pubmed_book_reads_its_publication_types(whole_book: bool) -> None:
    book = _genereviews_chapter()
    if whole_book:
        book.book_document.ClearField('article_title')
    book.book_document.publication_type.add(ui='D016454', value='Review')
    fields = metadata_fields.read_pubmed(litcache_pb2.PubmedRecord(book_article=book))
    assert list(fields.publication_types) == ['Review']


def test_the_pubmed_fixture_keeps_every_publication_type_it_states() -> None:
    record = _pubmed_article()
    stated = [t.value for t in record.article.medline_citation.article.publication_type_list]
    assert stated  # rules out a vacuous pass
    assert list(metadata_fields.read_pubmed(record).publication_types) == stated


@pytest.mark.parametrize(
    ('work_type', 'expected'),
    [
        ('article', ['Journal Article']),
        ('review', ['Review']),
        ('preprint', ['Preprint']),
        ('letter', ['Letter']),
        ('erratum', ['Published Erratum']),
        ('retraction', ['Retraction Notice']),
        ('editorial', []),
        ('book-chapter', []),
        (None, []),
    ],
)
def test_an_openalex_type_maps_onto_nlms_by_a_fixed_table(work_type: str | None, expected: list[str]) -> None:
    assert list(metadata_fields.read_openalex({'title': 'T', 'type': work_type}).publication_types) == expected


@pytest.mark.parametrize(
    ('work', 'expected'),
    [
        ({'type': 'journal-article'}, ['Journal Article']),
        ({'type': 'journal-article', 'subtype': None}, ['Journal Article']),
        ({'type': 'posted-content', 'subtype': 'preprint'}, ['Preprint']),
        ({'type': 'posted-content'}, []),
        ({'type': 'posted-content', 'subtype': 'other'}, []),
        ({'type': 'book-chapter'}, []),
        ({'type': 'editorial'}, []),
        ({'type': None}, []),
        ({}, []),
    ],
)
def test_a_crossref_type_maps_onto_nlms_by_a_fixed_table(work: dict[str, object], expected: list[str]) -> None:
    assert list(metadata_fields.read_crossref(work).publication_types) == expected


@pytest.mark.parametrize(
    ('index', 'work', 'path'),
    [
        ('openalex', {'title': 'T', 'type': 5}, r'type is a number'),
        ('crossref', {'type': ['journal-article']}, r'type is an array'),
        ('crossref', {'type': 'posted-content', 'subtype': 5}, r'subtype is a number'),
    ],
)
def test_a_type_of_the_wrong_json_type_drifts(index: str, work: dict[str, object], path: str) -> None:
    reader = metadata_fields.read_crossref if index == 'crossref' else metadata_fields.read_openalex
    with pytest.raises(upstream_record.SchemaDriftError, match=path):
        reader(work)
