"""The pattern scan's hits, and the toolbox's accounting of which are covered, dismissed or outstanding."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from themis.litcache import variant_reference, variant_scan, variant_tools
from themis.litcache.models import variants_pb2


def _texts(text: str) -> list[str]:
    return [hit.text for hit in variant_scan.scan(text)]


@pytest.mark.parametrize(
    ('text', 'hit'),
    [
        ('carried c.1691G>A, a missense change', 'c.1691G>A'),
        ('the variant NM_000059.4:c.1691G>A was', 'NM_000059.4:c.1691G>A'),
        ('the variant BRCA2:c.1691G>A was', 'BRCA2:c.1691G>A'),
        ('a stop, p.(Arg564*).', 'p.(Arg564*)'),
        ('the change R337H segregated', 'R337H'),
        ('the change Arg337His segregated', 'Arg337His'),
        ('a 1691G>A transition', '1691G>A'),
        ('splicing at IVS8-1G>C', 'IVS8-1G>C'),
        ('the SNP rs121913529 was', 'rs121913529'),
        ('a nonsense change R337* in exon 4', 'R337*'),
        ('the change p.(Gln\u2010486Glu) was', 'p.(Gln\u2010486Glu)'),
        ('the intronic c.123\u22122A>G change', 'c.123\u22122A>G'),
        ('a deep intronic c.2991+1655A\u2192G change', 'c.2991+1655A\u2192G'),
        ('a spaced c.1688T > C change', 'c.1688T > C'),
        ('carried c.1A>G\u00a0in exon 1', 'c.1A>G'),
        ('the 1-22199112-C-T variant', '1-22199112-C-T'),
        ('| 140 | Asp\u2192Asn | heterozygous |', '140 | Asp\u2192Asn'),
        ('| 470 | G\u2192A | heterozygous |', '470 | G\u2192A'),
        ('| P3 | Asp\u2192Asn | heterozygous |', 'Asp\u2192Asn'),
        ('a 450 delA frameshift', '450 delA'),
        ('codon 64, Arg\u2192stop, truncates', 'Arg\u2192stop'),
        ('the prothrombin G20210A allele', 'G20210A'),
        ('LHON with T14484C', 'T14484C'),
        ('homozygous for \u0394F508 as', '\u0394F508'),
        ('homozygous for delta F508 as', 'delta F508'),
        ('homozygous for delta-F508 as', 'delta-F508'),
        ('homozygous for F508\u0394 as', 'F508\u0394'),
        ('a truncating Arg72stop', 'Arg72stop'),
        ('a truncating R72stop', 'R72stop'),
        ('the frameshift Pro72ArgfsTer10 in', 'Pro72ArgfsTer10'),
        ('the frameshift P72Rfs*10 in', 'P72Rfs*10'),
        ('beta-thalassaemia IVS-I-110 (G>A) and', 'IVS-I-110 (G>A)'),
        ('beta-thalassaemia IVS-II-654 and', 'IVS-II-654'),
        ('the change C.215C>G was', 'C.215C>G'),
    ],
)
def test_scan_flags_variant_notation(text: str, hit: str) -> None:
    assert _texts(text) == [hit]


@pytest.mark.parametrize(
    'text',
    [
        'as described, e.g. in reference 3',
        'see p. 12 of the supplement',
        'patient **P1** was the proband',
        'the variant lies in IVS12 of the gene',
        'raised HbA1c and G6PD deficiency',
        'the change in \u0394R2 was 0.3, and \u0394T1 fell',
    ],
)
def test_scan_ignores_what_is_not_variant_notation(text: str) -> None:
    assert _texts(text) == []


def test_a_damaged_description_is_flagged_as_hgvs_like() -> None:
    (hit,) = variant_scan.scan('a stop, p.(Gln\u2010395a), in exon 2')
    assert (hit.text, hit.kind) == ('p.(Gln\u2010395a)', 'HGVS-like')


def test_a_page_reference_is_not_hgvs_like() -> None:
    assert _texts('as on p.23, and c.1990') == []


def test_a_short_hit_is_covered_only_where_it_is_printed() -> None:
    (hit,) = [h for h in variant_scan.scan('a 418 G\u2192A change') if h.text == '418 G\u2192A']
    assert variant_scan.covers('418 G\u2192A', hit)
    (short,) = variant_scan.scan('splicing, +1G>A, here')
    assert variant_scan.covers('c.123+1G>A', short)
    assert not variant_scan.covers('c.11G>A', short)


def test_a_short_mention_does_not_cover_a_longer_hit() -> None:
    (hit,) = variant_scan.scan('| 470 | G\u2192A |')
    assert variant_scan.covers('470 G\u2192A', hit)
    assert not variant_scan.covers('G\u2192A', hit)


def test_a_description_does_not_take_in_the_next_word() -> None:
    assert _texts('the c.100del A novel variant') == ['c.100del']


def test_a_loose_match_inside_an_hgvs_match_is_not_flagged_again() -> None:
    assert _texts('the change p.R337H was') == ['p.R337H']


def test_a_hit_counts_its_printings_and_keeps_its_first_line() -> None:
    hits = variant_scan.scan('intro\nR337H here\nand R337H again\nc.5C>T last')
    assert [(h.id, h.text, h.line, h.printings) for h in hits] == [(1, 'R337H', 2, 2), (2, 'c.5C>T', 4, 1)]


def test_a_mention_recording_part_of_a_prefixed_hit_covers_it() -> None:
    (hit,) = variant_scan.scan('NM_000059.4:c.1691G>A')
    assert variant_scan.covers('c.1691G>A', hit)
    assert not variant_scan.covers('c.1691G>T', hit)


def _entry(verbatim: str, line: int = 1, context: str = 'observed') -> dict[str, Any]:
    return {
        'verbatim': verbatim,
        'gene': 'ABC1',
        'line': line,
        'context': context,
        'disposition': 'has_candidates',
        'reason': '',
        'candidates': [],
    }


def _toolbox(text: str) -> variant_tools.Toolbox:
    # Recording and dismissing never read the reference stores.
    references = cast(variant_reference.References, object())
    return variant_tools.Toolbox(references, markdown=text, pdf_bytes=None, hits=variant_scan.scan(text))


def test_hits_are_outstanding_until_recorded_or_dismissed() -> None:
    toolbox = _toolbox('R337H in P3; see Figure S1D')
    assert [h.text for h in toolbox.outstanding()] == ['R337H', 'S1D']
    toolbox.call('record_mentions', {'mentions': [_entry('R337H')]})
    toolbox.call('dismiss_hits', {'dismissals': [{'hit': 2, 'reason': 'a figure panel label'}]})
    assert toolbox.outstanding() == []
    assert [(d.text, d.line, d.reason) for d in toolbox.mention_set.dismissals] == [('S1D', 1, 'a figure panel label')]


def test_dismissing_an_unknown_hit_is_an_input_error() -> None:
    toolbox = _toolbox('R337H')
    with pytest.raises(variant_tools.ToolInputError, match='numbered 9'):
        toolbox.call('dismiss_hits', {'dismissals': [{'hit': 9, 'reason': 'no such hit'}]})


def test_without_a_scan_there_is_no_dismiss_tool_and_nothing_outstanding() -> None:
    toolbox = variant_tools.Toolbox(
        cast(variant_reference.References, object()), markdown=None, pdf_bytes=None, hits=None
    )
    assert 'dismiss_hits' not in {tool['name'] for tool in toolbox.definitions}
    assert toolbox.outstanding() == []


def test_each_occurrence_keeps_its_own_context() -> None:
    toolbox = _toolbox('R337H was reported [4]\nP3 carried R337H')
    toolbox.call('record_mentions', {'mentions': [_entry('R337H', 1, 'cited'), _entry('R337H', 2, 'observed')]})
    (mention,) = toolbox.mention_set.mentions
    assert [o.context for o in mention.occurrences] == [variants_pb2.CONTEXT_CITED, variants_pb2.CONTEXT_OBSERVED]


def test_a_string_without_a_position_is_a_mention_per_row() -> None:
    toolbox = _toolbox('| 98 | Arg\u2192His | homozygous |\n| 157 | Arg\u2192His | heterozygous |')
    first, second, again = _entry('Arg\u2192His', 1), _entry('Arg\u2192His', 2), _entry('Arg\u2192His', 1)
    toolbox.call('record_mentions', {'mentions': [first, second, again]})
    assert [len(m.occurrences) for m in toolbox.mention_set.mentions] == [2, 1]


def test_a_string_that_parses_as_hgvs_cannot_be_marked_not_writable() -> None:
    toolbox = _toolbox('| p.M358fs |\na deletion of exon 3')
    entry = _entry('p.M358fs') | {'disposition': 'not_writable', 'reason': 'garbled table text'}
    with pytest.raises(variant_tools.ToolInputError, match='writable'):
        toolbox.call('record_mentions', {'mentions': [entry]})
    prose = _entry('a deletion of exon 3', 2) | {
        'disposition': 'not_writable',
        'reason': 'exon boundaries not stated',
    }
    toolbox.call('record_mentions', {'mentions': [prose]})


def test_printed_accessions_gather_versions_and_printings() -> None:
    text = 'numbered on NM_000059.4 (NP_000050.3)\nand NM_000059 elsewhere; built on NC_000013.11; ENST00000380152.8'
    printed = {a.base: a for a in variant_scan.printed_accessions(text)}
    assert set(printed) == {'NM_000059', 'NP_000050', 'ENST00000380152'}
    brca2 = printed['NM_000059']
    assert (brca2.versions, brca2.unversioned, brca2.line, brca2.printings) == (('4',), True, 1, 2)


class _Unparseable:
    def check(self, expression: str) -> variant_reference.Check:  # noqa: ARG002 — every expression is unparseable here
        return variant_reference.Check(variants_pb2.OUTCOME_UNPARSEABLE, 'ParseError')


@pytest.mark.parametrize(
    ('expression', 'advice'),
    [
        ('NM_1.1:c.[104-5C>G];[787delG]', 'alleles in trans'),
        ('NM_1.1:c.841C>T(p.Arg281Trp)', 'one expression per candidate'),
    ],
)
def test_check_says_what_to_do_with_an_unparseable_shape(expression: str, advice: str) -> None:
    toolbox = variant_tools.Toolbox(
        cast(variant_reference.References, _Unparseable()), markdown=None, pdf_bytes=None, hits=None
    )
    (result,) = json.loads(cast(str, toolbox.call('check', {'expressions': [expression]})))
    assert advice in result['detail']
    assert result['detail'].endswith('ParseError')


def test_a_mention_recorded_with_candidates_promised_and_none_written_is_unwritten() -> None:
    toolbox = _toolbox('W115G')
    toolbox.call('record_mentions', {'mentions': [_entry('W115G')]})
    assert [m.verbatim for m in toolbox.unwritten()] == ['W115G']


def test_an_entry_whose_line_does_not_print_it_is_returned_and_the_rest_recorded() -> None:
    toolbox = _toolbox('P3 carried R337H\nand P4 carried\nc.1691G>A')
    result = toolbox.call(
        'record_mentions', {'mentions': [_entry('R337H'), _entry('c.1691G>A', 2), _entry('W115G', 9)]}
    )
    assert [m.verbatim for m in toolbox.mention_set.mentions] == ['R337H', 'c.1691G>A']
    assert "'W115G' on line 9" in cast(str, result)


def test_a_mention_recorded_without_a_species_is_human_and_cited_by_its_line() -> None:
    toolbox = _toolbox('R337H')
    toolbox.call('record_mentions', {'mentions': [_entry('R337H')]})
    (mention,) = toolbox.mention_set.mentions
    assert mention.known_species == variants_pb2.SPECIES_HUMAN
    assert [(o.line, o.HasField('page')) for o in mention.occurrences] == [(1, False)]


@pytest.mark.parametrize(
    ('markdown', 'pdf_bytes', 'located_by'),
    [('R337H', None, {'line'}), (None, b'%PDF', {'page', 'box'}), ('R337H', b'%PDF', {'line', 'page', 'box'})],
)
def test_an_entry_is_located_by_what_the_reading_holds(
    markdown: str | None, pdf_bytes: bytes | None, located_by: set[str]
) -> None:
    toolbox = variant_tools.Toolbox(
        cast(variant_reference.References, object()), markdown=markdown, pdf_bytes=pdf_bytes, hits=None
    )
    (tool,) = [t for t in toolbox.definitions if t['name'] == 'record_mentions']
    entry = cast(dict[str, Any], tool['input_schema'])['properties']['mentions']['items']
    assert {'line', 'page', 'box'} & set(entry['properties']) == located_by
    assert 'species' not in entry['required']
