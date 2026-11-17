"""The variant extraction agent: conversations over renderings, printed references, unfinished work, and faults."""

from __future__ import annotations

import asyncio
import collections.abc
import copy
import types
import typing

import anthropic
import anthropic.types
import httpx2
import pytest

from themis.litcache import ocr, variant_extract, variant_reference, variant_scan, variant_tools
from themis.litcache.models import variants_pb2
from themis.testing import litcache_variants


@pytest.fixture(scope='module')
def references(tmp_path_factory: pytest.TempPathFactory) -> variant_reference.References:
    return litcache_variants.build_references(tmp_path_factory.mktemp('variant-extract-refs'))


def _message(
    stop_reason: str | None, content: collections.abc.Sequence[anthropic.types.ContentBlock]
) -> anthropic.types.Message:
    return anthropic.types.Message(
        id='msg',
        type='message',
        role='assistant',
        model=variant_extract.MODEL,
        content=list(content),
        stop_reason=stop_reason,  # pyright: ignore[reportArgumentType]
        stop_sequence=None,
        usage=anthropic.types.Usage(input_tokens=1000, output_tokens=10),
    )


def _tool_turn(*calls: tuple[str, str, dict[str, object]]) -> anthropic.types.Message:
    blocks = [
        anthropic.types.ToolUseBlock(id=call_id, type='tool_use', name=name, input=arguments)
        for call_id, name, arguments in calls
    ]
    return _message('tool_use', blocks)


def _end_turn(summary: str = 'Done.') -> anthropic.types.Message:
    return _message('end_turn', [anthropic.types.TextBlock(type='text', text=summary)])


class _Stream:
    request_id = 'req'

    def __init__(self, message: anthropic.types.Message | None) -> None:
        # None simulates a connection dropping after the model began streaming.
        self._message = message
        self.current_message_snapshot = message or _message(None, [])
        self._sent = False

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self) -> typing.Self:
        return self

    async def __anext__(self) -> types.SimpleNamespace:
        if self._sent:
            if self._message is None:
                raise httpx2.ReadError('connection reset')
            raise StopAsyncIteration
        self._sent = True
        return types.SimpleNamespace(type='message_start')

    async def get_final_message(self) -> anthropic.types.Message:
        assert self._message is not None
        return self._message


class _Client:
    def __init__(self, turns: collections.abc.Sequence[anthropic.types.Message | None]) -> None:
        self.messages = self
        self._turns = list(turns)
        self.requests: list[dict[str, object]] = []

    def stream(self, **request: object) -> _Stream:
        self.requests.append(copy.deepcopy(request))
        return _Stream(self._turns.pop(0))


class _Tokens:
    def __init__(self) -> None:
        self.stops: list[str] = []

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        del usage, model
        self.stops.append(stop_reason)


def _opening_text(client: _Client) -> str:
    messages = typing.cast('list[dict[str, object]]', client.requests[0]['messages'])
    content = typing.cast('list[dict[str, str]]', messages[0]['content'])
    return '\n'.join(block['text'] for block in content if block['type'] == 'text')


def _last_user_content(client: _Client, request_index: int) -> object:
    messages = typing.cast('list[dict[str, object]]', client.requests[request_index]['messages'])
    return messages[-1]['content']


def test_extract_groups_printed_accessions_by_gene_with_their_proteins(
    references: variant_reference.References,
) -> None:
    markdown = 'NM_000101.1 and NP_000101.1\nNM_000200.3 for VER1, NM_000101 again, and NM_999999.1'
    client = _Client([_end_turn()])

    extraction = asyncio.run(
        variant_extract.extract(
            client=typing.cast('anthropic.AsyncAnthropic', client),
            references=references,
            reading='markdown',
            markdown=markdown,
            pdf_bytes=None,
            tokens=_Tokens(),
        )
    )

    opening = _opening_text(client)
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
    client = _Client([_end_turn()])

    asyncio.run(
        variant_extract.extract(
            client=typing.cast('anthropic.AsyncAnthropic', client),
            references=references,
            reading='markdown',
            markdown='No accessions or variants here.\n',
            pdf_bytes=None,
            tokens=_Tokens(),
        )
    )

    assert '<printed_references>\nnone\n</printed_references>' in _opening_text(client)


def test_extract_holds_the_conversation_open_until_outstanding_scan_hits_are_settled(
    references: variant_reference.References,
) -> None:
    client = _Client(
        [
            _end_turn('Premature summary.'),
            _tool_turn(('t1', 'dismiss_hits', {'dismissals': [{'hit': 1, 'reason': 'panel label'}]})),
            _end_turn('Done.'),
        ]
    )

    extraction = asyncio.run(
        variant_extract.extract(
            client=typing.cast('anthropic.AsyncAnthropic', client),
            references=references,
            reading='markdown',
            markdown='See Figure S1D.\n',
            pdf_bytes=None,
            tokens=_Tokens(),
        )
    )

    reminder = str(_last_user_content(client, 1))
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
    client = _Client(
        [
            _tool_turn(('t1', 'record_mentions', {'mentions': [unwritten]})),
            _end_turn('Premature summary.'),
            _tool_turn(('t2', 'record_mentions', {'mentions': [settled]})),
            _end_turn('Extracted 1 mention.'),
        ]
    )

    extraction = asyncio.run(
        variant_extract.extract(
            client=typing.cast('anthropic.AsyncAnthropic', client),
            references=references,
            reading='markdown',
            markdown='The patient had exon 3 deletion in GENE1.\n',
            pdf_bytes=None,
            tokens=_Tokens(),
        )
    )

    reminder = str(_last_user_content(client, 2))
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
    client = _Client(
        [
            _tool_turn(('t1', 'check', {'expressions': ['NP_000101.1:p.Lys2Arg']})),
            _end_turn(),
        ]
    )

    extraction = asyncio.run(
        variant_extract.extract(
            client=typing.cast('anthropic.AsyncAnthropic', client),
            references=references,
            reading='markdown',
            markdown='No scan hits here.\n',
            pdf_bytes=None,
            tokens=_Tokens(),
        )
    )

    (tool_reply,) = typing.cast('list[dict[str, object]]', _last_user_content(client, 1))
    assert tool_reply['tool_use_id'] == 't1'
    assert tool_reply['is_error'] is True
    assert 'RuntimeError' in str(tool_reply['content'])
    assert extraction.turns == 2
    assert extraction.tool_calls['check'] == 1


def test_a_turn_that_fails_after_the_model_began_answering_is_counted_and_raised_as_spent(
    references: variant_reference.References,
) -> None:
    tokens = _Tokens()
    with pytest.raises(ocr.SpentError):
        asyncio.run(
            variant_extract.extract(
                client=typing.cast('anthropic.AsyncAnthropic', _Client([None])),
                references=references,
                reading='markdown',
                markdown='No variants here.\n',
                pdf_bytes=None,
                tokens=tokens,
            )
        )
    assert tokens.stops == ['unfinished']


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
