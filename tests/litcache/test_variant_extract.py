"""The extraction harness: the printed-references table, the note before finishing, and how a failed turn ends."""

from __future__ import annotations

import asyncio
import collections
import types
from typing import Self, cast

import anthropic
import anthropic.types
import httpx2
import pytest

from themis.litcache import ocr, variant_extract, variant_reference, variant_scan, variant_tools
from themis.litcache.models import variants_pb2


class _Stores:
    def gene_symbol(self, accession: str) -> str | None:
        return {'NM_000059': 'BRCA2', 'NM_000059.4': 'BRCA2', 'NP_000050.3': 'BRCA2', 'NM_007294.4': 'BRCA1'}.get(
            accession
        )

    def held_versions(self, base: str) -> list[str]:
        return ['NM_000059.3', 'NM_000059.4'] if base == 'NM_000059' else []

    def protein_of(self, accession: str) -> str | None:
        return {'NM_000059.4': 'NP_000050.3', 'NM_007294.4': 'NP_009225.1'}.get(accession)


def test_the_table_lists_every_printed_accession_under_its_gene_with_its_protein() -> None:
    text = 'NM_000059.4 and NP_000050.3\nNM_007294.4 for BRCA1, NM_000059 again, and NM_999999.1'
    table = variant_extract._printed_references(
        variant_scan.printed_accessions(text), cast(variant_reference.References, _Stores())
    )
    rows = table.splitlines()[1:-1]
    assert rows == [
        (
            'BRCA2: NM_000059.4, NM_000059 (printed 2 times, first on line 1; its protein NP_000050.3); '
            'NP_000050.3 (printed once, first on line 1)'
        ),
        'BRCA1: NM_007294.4 (printed once, first on line 2; its protein NP_009225.1)',
        'gene not found in the reference stores: NM_999999.1 (printed once, first on line 2)',
    ]


def test_a_paper_printing_no_accession_says_so() -> None:
    table = variant_extract._printed_references([], cast(variant_reference.References, _Stores()))
    assert table == '<printed_references>\nnone\n</printed_references>'


def test_the_note_before_finishing_names_the_mentions_left_without_candidates() -> None:
    unwritten = [variants_pb2.Mention(verbatim='W115G', gene='NPR2')]
    note = variant_extract._unfinished_note([], unwritten)
    assert 'W115G\tNPR2' in note
    assert 'scan hits' not in note


def _message(stop_reason: str | None) -> anthropic.types.Message:
    return anthropic.types.Message(
        id='msg',
        type='message',
        role='assistant',
        model=variant_extract.MODEL,
        content=[],
        stop_reason=stop_reason,  # pyright: ignore[reportArgumentType]
        stop_sequence=None,
        usage=anthropic.types.Usage(input_tokens=1000, output_tokens=10),
    )


class _DroppedStream:
    """A turn the model began answering, whose connection then drops."""

    request_id = 'req'
    current_message_snapshot = _message(None)

    def __init__(self) -> None:
        self._sent = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> types.SimpleNamespace:
        if self._sent:
            raise httpx2.ReadError('connection reset')
        self._sent = True
        return types.SimpleNamespace(type='message_start')


class _Client:
    def __init__(self) -> None:
        self.messages = self

    def stream(self, **_: object) -> _DroppedStream:
        return _DroppedStream()


class _Tokens:
    def __init__(self) -> None:
        self.stops: list[str] = []

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        del usage, model
        self.stops.append(stop_reason)


def test_a_turn_that_fails_after_the_model_began_answering_is_counted_and_raised_as_spent() -> None:
    tokens = _Tokens()
    with pytest.raises(ocr.SpentError):
        asyncio.run(
            variant_extract.extract(
                client=cast(anthropic.AsyncAnthropic, _Client()),
                references=cast(variant_reference.References, _Stores()),
                reading='markdown',
                markdown='No variants here.\n',
                pdf_bytes=None,
                tokens=tokens,
            )
        )
    assert tokens.stops == ['unfinished']


class _BrokenToolbox:
    def call(self, name: str, arguments: object) -> str:
        del name, arguments
        raise RuntimeError('a store read failed')


def test_a_tool_that_fails_answers_its_call_as_an_error_and_the_conversation_goes_on() -> None:
    call = anthropic.types.ToolUseBlock(id='t1', type='tool_use', name='check', input={'expressions': []})

    results = asyncio.run(
        variant_extract._run_tools(cast(variant_tools.Toolbox, _BrokenToolbox()), [call], collections.Counter())
    )

    assert [(r['tool_use_id'], r.get('is_error')) for r in results] == [('t1', True)]


@pytest.mark.parametrize(
    ('module', 'name', 'changed'),
    [
        (variant_extract, 'MODEL', 'claude-other'),
        (variant_extract, '_EFFORT', 'high'),
        (variant_extract, '_SYSTEM', 'Extract nothing.'),
        (variant_tools, '_TOOLS', []),
        (variant_scan, '_LOOSE', {}),
    ],
)
def test_a_change_to_what_the_agent_is_given_is_a_new_harness_version(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, name: str, changed: object
) -> None:
    # A mention set made under another version is extraction work: a missed bump would leave stale sets current.
    before = variant_extract._harness_version()
    monkeypatch.setattr(module, name, changed)

    assert variant_extract._harness_version() != before
