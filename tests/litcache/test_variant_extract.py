"""The variant extraction agent: conversations over renderings, printed references, unfinished work, and faults."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

import anthropic
import pytest

from themis.litcache import ocr, variant_extract, variant_reference, variant_scan, variant_tools
from themis.litcache.models import variants_pb2
from themis.testing import anthropic_sse, litcache_variants


@pytest.fixture(scope='module')
def references(tmp_path_factory: pytest.TempPathFactory) -> variant_reference.References:
    return litcache_variants.build_references(tmp_path_factory.mktemp('variant-extract-refs'))


def _tool_turn(*calls: tuple[str, str, dict[str, object]]) -> anthropic_sse.MessageTurn:
    blocks = [anthropic_sse.ToolUse(id=call_id, name=name, input=arguments) for call_id, name, arguments in calls]
    return anthropic_sse.MessageTurn(stop_reason='tool_use', content=blocks, model=variant_extract.MODEL)


def _end_turn(summary: str = 'Done.') -> anthropic_sse.MessageTurn:
    return anthropic_sse.MessageTurn(
        stop_reason='end_turn',
        content=[anthropic_sse.Text(summary)],
        model=variant_extract.MODEL,
    )


class _Tokens:
    def __init__(self) -> None:
        self.stops: list[str] = []

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        del usage, model
        self.stops.append(stop_reason)


def _extract(
    harness: anthropic_sse.Harness,
    references: variant_reference.References,
    *,
    markdown: str,
    tokens: _Tokens | None = None,
) -> variant_extract.Extraction:
    async def run() -> variant_extract.Extraction:
        async with harness.client() as client:
            return await variant_extract.extract(
                client=client,
                references=references,
                reading='markdown',
                markdown=markdown,
                pdf_bytes=None,
                tokens=tokens or _Tokens(),
            )

    return asyncio.run(run())


def _opening_text(harness: anthropic_sse.Harness) -> str:
    blocks = harness.requests[0].content_blocks(0)
    return '\n'.join(str(block['text']) for block in blocks if block['type'] == 'text')


def test_extract_groups_printed_accessions_by_gene_with_their_proteins(
    references: variant_reference.References,
) -> None:
    markdown = 'NM_000101.1 and NP_000101.1\nNM_000200.3 for VER1, NM_000101 again, and NM_999999.1'
    harness = anthropic_sse.Harness([_end_turn()])

    extraction = _extract(harness, references, markdown=markdown)

    opening = _opening_text(harness)
    assert (
        'GENE1: NM_000101.1, NM_000101 (printed 2 times, first on line 1; its protein NP_000101.1); '
        'NP_000101.1 (printed once, first on line 1)'
    ) in opening
    assert 'VER1: NM_000200.3 (printed once, first on line 2; its protein NP_000200.3)' in opening
    assert 'gene not found in the reference stores: NM_999999.1 (printed once, first on line 2)' in opening
    assert extraction.turns == 1
    assert extraction.construction == variants_pb2.VariantExtractionConstruction(
        model=variant_extract.MODEL, harness_version=variant_extract.HARNESS_VERSION
    )


def test_extract_says_none_when_the_paper_prints_no_accession(references: variant_reference.References) -> None:
    harness = anthropic_sse.Harness([_end_turn()])

    _extract(harness, references, markdown='No accessions or variants here.\n')

    assert '<printed_references>\nnone\n</printed_references>' in _opening_text(harness)


def test_extract_holds_the_conversation_open_until_outstanding_scan_hits_are_settled(
    references: variant_reference.References,
) -> None:
    harness = anthropic_sse.Harness(
        [
            _end_turn('Premature summary.'),
            _tool_turn(('t1', 'dismiss_hits', {'dismissals': [{'hit': 1, 'reason': 'panel label'}]})),
            _end_turn('Done.'),
        ]
    )

    extraction = _extract(harness, references, markdown='See Figure S1D.\n')

    reminder = harness.requests[1].messages[-1]['content']
    assert isinstance(reminder, str)
    assert '1 scan hits are neither printed by a recorded mention nor dismissed' in reminder
    assert 'S1D' in reminder
    assert extraction.turns == 3
    assert [d.text for d in extraction.mention_set.dismissals] == ['S1D']


def test_extract_holds_the_conversation_open_until_unwritten_mentions_are_completed(
    references: variant_reference.References,
) -> None:
    unwritten: dict[str, object] = {
        'verbatim': 'exon 3 deletion',
        'gene': 'GENE1',
        'line': 1,
        'context': 'observed',
        'disposition': 'has_candidates',
        'reason': '',
        'candidates': [],
    }
    settled: dict[str, object] = {
        **unwritten,
        'disposition': 'not_writable',
        'reason': 'exon boundaries are not stated',
    }
    harness = anthropic_sse.Harness(
        [
            _tool_turn(('t1', 'record_mentions', {'mentions': [unwritten]})),
            _end_turn('Premature summary.'),
            _tool_turn(('t2', 'record_mentions', {'mentions': [settled]})),
            _end_turn('Extracted 1 mention.'),
        ]
    )

    extraction = _extract(harness, references, markdown='The patient had exon 3 deletion in GENE1.\n')

    reminder = harness.requests[2].messages[-1]['content']
    assert isinstance(reminder, str)
    assert 'exon 3 deletion\tGENE1' in reminder
    assert 'scan hits' not in reminder
    assert extraction.turns == 4
    assert extraction.mention_set.mentions[0].disposition == variants_pb2.DISPOSITION_NOT_WRITABLE


def test_a_tool_that_fails_answers_its_call_as_an_error_and_the_conversation_goes_on(
    references: variant_reference.References, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_check(_expression: str) -> variant_reference.Check:
        raise RuntimeError('a store read failed')

    monkeypatch.setattr(references, 'check', broken_check)
    harness = anthropic_sse.Harness(
        [
            _tool_turn(('t1', 'check', {'expressions': ['NP_000101.1:p.Lys2Arg']})),
            _end_turn(),
        ]
    )

    extraction = _extract(harness, references, markdown='No scan hits here.\n')

    (tool_reply,) = harness.requests[1].content_blocks(-1)
    assert tool_reply['tool_use_id'] == 't1'
    assert tool_reply['is_error'] is True
    assert 'RuntimeError' in str(tool_reply['content'])
    assert extraction.turns == 2
    assert extraction.tool_calls['check'] == 1


def test_a_turn_that_fails_after_the_model_began_answering_is_counted_and_raised_as_spent(
    references: variant_reference.References,
) -> None:
    tokens = _Tokens()
    harness = anthropic_sse.Harness([anthropic_sse.DroppedStream(model=variant_extract.MODEL)])

    with pytest.raises(ocr.SpentError):
        _extract(harness, references, markdown='No variants here.\n', tokens=tokens)

    assert tokens.stops == ['unfinished']


def test_a_change_to_what_the_agent_is_given_is_a_new_harness_version() -> None:
    assert re.fullmatch(r'[1-9]\d*\.[0-9a-f]{12}', variant_extract.HARNESS_VERSION)
    assert variant_extract.harness_version() == variant_extract.HARNESS_VERSION
    assert variant_extract.harness_version(model='claude-other') != variant_extract.HARNESS_VERSION
    assert variant_extract.harness_version(tool_surface=[]) != variant_extract.HARNESS_VERSION
    assert variant_extract.harness_version(scan_surface=[]) != variant_extract.HARNESS_VERSION


@pytest.mark.parametrize(
    ('markdown', 'pdf_bytes', 'hits'),
    [
        ('R337H', None, variant_scan.scan('R337H')),
        ('R337H', None, None),
        (None, b'%PDF', None),
        ('R337H', b'%PDF', None),
        ('R337H', b'%PDF', variant_scan.scan('R337H')),
    ],
)
def test_every_offered_tool_definition_is_in_the_tool_surface(
    references: variant_reference.References,
    markdown: str | None,
    pdf_bytes: bytes | None,
    hits: Sequence[variant_scan.Hit] | None,
) -> None:
    tool_surface = variant_tools.surface()
    toolbox = variant_tools.Toolbox(references, markdown=markdown, pdf_bytes=pdf_bytes, hits=hits)
    for definition in toolbox.definitions:
        assert definition in tool_surface
