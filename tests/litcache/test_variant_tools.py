"""The agent's recording tools: what a call may leave in the mention set, and what it must refuse."""

from __future__ import annotations

import collections
from typing import cast

import pytest

from themis.litcache import variant_reference, variant_tools
from themis.litcache.models import variants_pb2

_MARKDOWN = 'Methods\nThe patient carried exon 3 deletion in FWD.\n'


def _toolbox() -> variant_tools.Toolbox:
    # The recording tools never read the stores.
    return variant_tools.Toolbox(
        cast(variant_reference.References, object()), markdown=_MARKDOWN, pdf_bytes=None, hits=None
    )


def _entry(**fields: object) -> dict[str, object]:
    entry: dict[str, object] = {
        'verbatim': 'exon 3 deletion',
        'gene': 'FWD',
        'line': 2,
        'context': 'observed',
        'disposition': 'not_writable',
        'reason': 'an exon-level deletion with no breakpoints',
        'candidates': [],
    }
    entry.update(fields)
    return entry


def test_a_mention_first_recorded_with_no_candidates_can_be_recorded_again_as_what_it_is() -> None:
    toolbox = _toolbox()
    toolbox.call('record_mentions', {'mentions': [_entry(disposition='has_candidates', reason='')]})
    assert toolbox.unwritten()

    toolbox.call('record_mentions', {'mentions': [_entry()]})

    assert not toolbox.unwritten()
    [mention] = toolbox.mention_set.mentions
    assert mention.disposition == variants_pb2.DISPOSITION_NOT_WRITABLE


def test_a_float_where_an_integer_belongs_is_refused_as_input_the_agent_can_correct() -> None:
    with pytest.raises(variant_tools.ToolInputError, match='line'):
        _toolbox().call('record_mentions', {'mentions': [_entry(line=2.0)]})


def test_a_value_the_proto_cannot_hold_is_refused_and_records_nothing_of_its_call() -> None:
    # Pages are offered, so an entry may be read from a figure on a page; the page is past int32's range.
    toolbox = variant_tools.Toolbox(
        cast(variant_reference.References, object()), markdown=_MARKDOWN, pdf_bytes=b'%PDF', hits=None
    )
    text = _entry(page=None, box=None)
    toolbox.call('record_mentions', {'mentions': [text]})
    before = toolbox.mention_set.SerializeToString(deterministic=True)
    box = {'left': 0.1, 'top': 0.1, 'right': 0.5, 'bottom': 0.5}
    figure = _entry(verbatim='exon 5 duplication', line=None, page=2**40, box=box)

    with pytest.raises(variant_tools.ToolInputError, match='nothing in this call was recorded'):
        toolbox.call('record_mentions', {'mentions': [figure]})

    assert toolbox.mention_set.SerializeToString(deterministic=True) == before
    # The rolled-back entry can be recorded again, corrected, as a mention of its own.
    toolbox.call('record_mentions', {'mentions': [{**figure, 'page': 3}]})
    assert len(toolbox.mention_set.mentions) == 2


def test_a_call_that_would_leave_an_invalid_mention_set_records_nothing() -> None:
    toolbox = _toolbox()
    toolbox.call('record_mentions', {'mentions': [_entry()]})
    before = toolbox.mention_set.SerializeToString(deterministic=True)
    declarations = {'references': [], 'builds': [{'build': '', 'sentence': 'on GRCh38'}], 'conventions': []}

    with pytest.raises(variant_tools.ToolInputError, match='nothing in this call was recorded'):
        toolbox.call('record_declarations', declarations)

    assert toolbox.mention_set.SerializeToString(deterministic=True) == before


def test_an_entry_the_markdown_prints_is_cited_by_its_line_not_also_its_page() -> None:
    toolbox = variant_tools.Toolbox(
        cast(variant_reference.References, object()), markdown=_MARKDOWN, pdf_bytes=b'%PDF', hits=None
    )
    with pytest.raises(variant_tools.ToolInputError, match='exactly one'):
        toolbox.call('record_mentions', {'mentions': [_entry(page=1, box=None)]})
    assert not toolbox.mention_set.mentions


def test_every_listed_species_has_its_enum_name_and_its_scientific_name() -> None:
    names: collections.defaultdict[int, set[str]] = collections.defaultdict(set)
    for name, value in variant_tools.SPECIES.items():
        names[value].add(name)
    listed = {v for v in variants_pb2.Species.values() if v != variants_pb2.SPECIES_UNSPECIFIED}
    assert set(names) == listed
    assert all(any(' ' in n for n in ns) and any(' ' not in n for n in ns) for ns in names.values())


@pytest.mark.parametrize(
    ('species', 'known', 'printed'),
    [
        (None, variants_pb2.SPECIES_HUMAN, ''),
        ('mouse', variants_pb2.SPECIES_MOUSE, ''),
        ('Mus  musculus', variants_pb2.SPECIES_MOUSE, ''),
        ('Xenopus laevis', variants_pb2.SPECIES_UNSPECIFIED, 'Xenopus laevis'),
    ],
)
def test_a_mention_records_a_listed_species_by_value_and_any_other_as_printed(
    species: str | None, known: variants_pb2.Species, printed: str
) -> None:
    toolbox = _toolbox()
    entry = _entry() if species is None else _entry(species=species)
    toolbox.call('record_mentions', {'mentions': [entry]})
    (mention,) = toolbox.mention_set.mentions
    assert (mention.known_species, mention.printed_species) == (known, printed)


def test_each_alternative_of_a_slash_form_is_a_mention_of_its_own_that_later_printings_merge_into() -> None:
    toolbox = variant_tools.Toolbox(
        cast(variant_reference.References, object()),
        markdown='Results\nCarriers of R482Q/W in LMNA.\nDiscussion\nR482Q/W recurs.\n',
        pdf_bytes=None,
        hits=None,
    )
    alleles = ['NP_733821.1:p.Arg482Gln', 'NP_733821.1:p.Arg482Trp']

    def entry(alternative: int, line: int, candidates: list[str]) -> dict[str, object]:
        return _entry(
            verbatim='R482Q/W',
            gene='LMNA',
            line=line,
            alternative=alternative,
            disposition='has_candidates',
            reason='',
            candidates=[{'hgvs': hgvs, 'flanking': None, 'rewrites': []} for hgvs in candidates],
        )

    toolbox.call('record_mentions', {'mentions': [entry(1, 2, alleles[:1]), entry(2, 2, alleles[1:])]})
    toolbox.call('record_mentions', {'mentions': [entry(2, 4, []), entry(1, 4, [])]})

    assert [(m.verbatim, [c.hgvs for c in m.candidates]) for m in toolbox.mention_set.mentions] == [
        ('R482Q/W', alleles[:1]),
        ('R482Q/W', alleles[1:]),
    ]
    assert [[o.line for o in m.occurrences] for m in toolbox.mention_set.mentions] == [[2, 4], [2, 4]]
