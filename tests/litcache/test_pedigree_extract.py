"""The pedigree reader's conversations: one per figure, its pages and zooms, the checks an answer must pass."""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import io
import json
import pathlib
import types
from collections.abc import Sequence
from typing import Any, Self, cast

import anthropic
import httpx2
import PIL.Image
import protovalidate
import pytest
from grus import ir
from grus.models import pedigree_pb2

from themis.litcache import claude_images, ocr, pdf, pedigree_extract

_PDF = (pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'oa' / 'source.pdf').read_bytes()
_PDF_HASH = 'c' * 64
_MARKDOWN = 'Results.\n**Figure 1** Pedigree of the family with retinitis pigmentosa, inherited as X-linked.\n'


def _answer_set(*, page: int = 1) -> dict[str, Any]:
    condition: dict[str, Any] = {'id': 'k1', 'name': 'retinitis pigmentosa'}
    supports: list[dict[str, Any]] = [{'field': 'condition.name', 'condition_id': 'k1', 'citations': ['c2']}]
    citations: list[dict[str, Any]] = [
        {'id': 'c1', 'region': {'page': page, 'x0': 10, 'y0': 20, 'x1': 200, 'y1': 150}},
        {'id': 'c2', 'quote': 'retinitis pigmentosa'},
    ]
    return {
        'conditions': [condition],
        'pedigrees': [
            {
                'individuals': [
                    {'generation': 1, 'index': 1, 'gender': 'GENDER_MAN'},
                    {'generation': 1, 'index': 2, 'gender': 'GENDER_WOMAN'},
                    {
                        'generation': 2,
                        'index': 1,
                        'gender': 'GENDER_WOMAN',
                        'proband': True,
                        'conditions': [{'condition_id': 'k1', 'status': 'CONDITION_STATUS_AFFECTED'}],
                    },
                ],
                'matings': [
                    {
                        'partner_a': {'generation': 1, 'index': 1},
                        'partner_b': {'generation': 1, 'index': 2},
                        'offspring': [{'child': {'generation': 2, 'index': 1}}],
                    }
                ],
                'evidence': ['c1'],
                'supports': supports,
            }
        ],
        'citations': citations,
    }


def _message(stop_reason: str | None, content: list[Any]) -> anthropic.types.Message:
    return anthropic.types.Message(
        id='msg',
        type='message',
        role='assistant',
        model=pedigree_extract.MODEL,
        content=content,
        stop_reason=stop_reason,  # pyright: ignore[reportArgumentType]
        stop_sequence=None,
        usage=anthropic.types.Usage(input_tokens=1000, output_tokens=10),
    )


def _answer(answer: dict[str, Any]) -> anthropic.types.Message:
    return _message('end_turn', [anthropic.types.TextBlock(type='text', text=json.dumps(answer))])


def _zoom(region: list[float], *, page: int = 1) -> anthropic.types.Message:
    call = anthropic.types.ToolUseBlock(type='tool_use', id='z1', name='zoom', input={'page': page, 'region': region})
    return _message('tool_use', [call])


class _Stream:
    request_id = 'req'

    def __init__(self, message: anthropic.types.Message | None) -> None:
        # None is a turn whose connection drops after the model began answering.
        self._message = message
        self.current_message_snapshot = message or _message(None, [])
        self._sent = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self) -> Self:
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
    """Answers each turn from a script, and keeps what each request carried."""

    def __init__(self, *turns: anthropic.types.Message | Exception | None) -> None:
        self.messages = self
        self._turns = list(turns)
        self.requests: list[list[Any]] = []

    def stream(self, **request: Any) -> _Stream:  # noqa: ANN401 — the SDK's keyword surface
        self.requests.append(copy.deepcopy(request['messages']))
        turn = self._turns.pop(0)
        if isinstance(turn, Exception):
            raise turn  # refused before the model answered
        return _Stream(turn)


class _Tokens:
    def __init__(self) -> None:
        self.stops: list[str] = []

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        del usage, model
        self.stops.append(stop_reason)


@pytest.fixture(autouse=True)
def _one_figure_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # The scripted client answers turns in order, so figures are read one after another.
    monkeypatch.setattr(pedigree_extract, '_CONCURRENCY', 1)


def _unit(figure: int, *pages: int, legend: int | None = None) -> pedigree_extract.FigurePages:
    """Figure `figure`, drawn on `pages` (page 1 when none are given)."""
    layouts = pdf.page_layouts(_PDF).result()
    return pedigree_extract.FigurePages(
        figure=figure,
        label=f'Figure {figure + 1}',
        pages=[pedigree_extract.DrawnPage(layouts[page - 1], [(0.1, 0.1, 0.9, 0.5)]) for page in pages or (1,)],
        legend=None if legend is None else layouts[legend - 1],
    )


def _read(
    client: _Client,
    *,
    units: list[pedigree_extract.FigurePages] | None = None,
    tokens: _Tokens | None = None,
    variants: Sequence[pedigree_extract.KnownVariant] = (),
) -> pedigree_extract.Reading:
    paper = pedigree_extract.PaperInput(
        markdown=_MARKDOWN,
        pdf_bytes=_PDF,
        pdf_hash=_PDF_HASH,
        figures=units or [_unit(0)],
        variants=variants,
    )
    return asyncio.run(
        pedigree_extract.read(
            paper, client=cast(anthropic.AsyncAnthropic, client), tokens=cast(ocr.TokenCounter, tokens or _Tokens())
        )
    )


def _overview_size(client: _Client) -> tuple[int, int]:
    data = client.requests[0][0]['content'][1]['source']['data']
    with PIL.Image.open(io.BytesIO(base64.standard_b64decode(data))) as image:
        return image.size


def test_a_page_read_with_a_zoom_is_placed_in_points_and_stamped_with_its_pdf() -> None:
    client = _Client(_zoom([0, 0, 200, 150]), _answer(_answer_set()))

    reading = _read(client)

    zoom_result = client.requests[1][-1]['content'][0]
    assert zoom_result['content'][0]['type'] == 'image'
    layout = pdf.page_layouts(_PDF).result()[0]
    width, height = _overview_size(client)
    region, quote = _only(reading).citations
    assert {region.document_id, quote.document_id} == {_PDF_HASH}
    assert region.region.page == 0
    assert region.region.x1 == pytest.approx(200 / width * layout.width, rel=1e-4)
    assert region.region.y1 == pytest.approx(150 / height * layout.height, rel=1e-4)
    assert [(r.figure, r.HasField('failure')) for r in reading.construction.figures_read] == [(0, False)]


def test_the_overview_reaches_the_model_at_the_size_it_reads_it() -> None:
    client = _Client(_answer({'pedigrees': []}))

    _read(client)

    width, height = _overview_size(client)
    tier = claude_images.HIGH_RESOLUTION
    assert claude_images.resized_size(width, height, max_edge=tier.max_edge, max_tokens=tier.max_tokens) == (
        width,
        height,
    )


def test_a_quote_the_paper_does_not_hold_goes_back_to_the_model_and_its_fix_is_kept() -> None:
    invented = _answer_set()
    invented['citations'][1]['quote'] = 'autosomal recessive inheritance'
    client = _Client(_answer(invented), _answer(_answer_set()))

    reading = _read(client)

    assert 'not in the paper' in client.requests[1][-1]['content']
    assert _only(reading).citations[1].quote == 'retinitis pigmentosa'


def test_a_zoom_too_small_to_render_goes_back_to_the_model_as_a_failed_call() -> None:
    client = _Client(_zoom([100, 100, 100.02, 100.02]), _answer(_answer_set()))

    reading = _read(client)

    result = client.requests[1][-1]['content'][0]
    assert result['is_error'] is True
    assert len(_only(reading).pedigrees) == 1


def test_a_reading_where_every_figure_fails_ends_without_a_record() -> None:
    client = _Client(_message('refusal', []))

    with pytest.raises(pedigree_extract.ExtractionError) as raised:
        _read(client)

    assert raised.value.refusal is not None
    assert raised.value.refusal['request_id'] == 'req'


def _genotyped(answer: dict[str, Any], *, allele: str = 'c.123A>G') -> dict[str, Any]:
    """The answer with its proband genotyped heterozygous at one locus, as the label beside them shows."""
    answer['loci'] = [
        {
            'id': 'l1',
            'name': 'c.123A>G',
            'gene': 'GENE1',
            'alleles': [{'symbol': '+', 'reference': True}, {'symbol': 'M', 'name': allele}],
        }
    ]
    proband = answer['pedigrees'][0]['individuals'][2]
    proband['genotypes'] = [{'locus_id': 'l1', 'alleles': ['M', '+']}]
    proband['annotations'] = [{'text': 'M/+', 'type': 'ANNOTATION_TYPE_GENOTYPE', 'loci': ['l1']}]
    return answer


def _only(reading: pedigree_extract.Reading) -> pedigree_pb2.PedigreeSet:
    """The one figure's set, for a reading of a single figure."""
    (one,) = reading.pedigrees.figures.values()
    return one


def _proband(pedigree: pedigree_pb2.Pedigree) -> pedigree_pb2.Individual:
    return next(i for i in pedigree.individuals if i.proband)


def test_each_figure_is_its_own_set_and_one_symbol_may_mean_different_variants_in_two() -> None:
    client = _Client(
        _answer(_genotyped(_answer_set())),
        _answer(_genotyped(_answer_set(), allele='c.800del')),
    )

    reading = _read(client, units=[_unit(0), _unit(1)])

    assert sorted(reading.pedigrees.figures) == [0, 1]
    names = []
    for one in reading.pedigrees.figures.values():
        (locus,) = one.loci
        names += [a.name for a in locus.alleles if a.symbol == 'M']
        assert len(_proband(one.pedigrees[0]).genotypes) == 1
        ir.validate_set(one)
    assert names == ['c.123A>G', 'c.800del']


def _unnamed(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer with its condition unnamed, as a figure whose legend names none declares it."""
    del answer['conditions'][0]['name']
    answer['pedigrees'][0]['supports'] = [
        s for s in answer['pedigrees'][0]['supports'] if s['field'] != 'condition.name'
    ]
    return answer


def test_figures_keep_their_conditions_apart_even_when_both_are_unnamed() -> None:
    client = _Client(_answer(_unnamed(_answer_set())), _answer(_unnamed(_answer_set())))

    reading = _read(client, units=[_unit(0), _unit(1)])

    sets = list(reading.pedigrees.figures.values())
    assert [[c.name for c in one.conditions] for one in sets] == [[''], ['']]


def test_a_figure_read_with_no_pedigree_on_it_is_recorded_with_an_empty_set() -> None:
    reading = _read(_Client(_answer({'pedigrees': []})))

    assert list(reading.pedigrees.figures) == [0]
    assert not reading.pedigrees.figures[0].pedigrees


def test_a_figure_on_two_pages_is_read_in_one_conversation_that_shows_both() -> None:
    client = _Client(_answer(_answer_set(page=2)))

    reading = _read(client, units=[_unit(0, 1, 2)])

    (request,) = client.requests
    first_turn = request[0]['content']
    assert [block['type'] for block in first_turn] == ['text', 'image', 'image', 'text']
    assert 'page 1 (' in first_turn[-1]['text']
    assert 'page 2 (' in first_turn[-1]['text']
    region = next(c.region for c in _only(reading).citations if c.HasField('region'))
    assert region.page == 1  # grus counts pages from 0


def test_a_region_on_a_page_not_shown_goes_back_to_the_model() -> None:
    client = _Client(_answer(_answer_set(page=3)), _answer(_answer_set()))

    reading = _read(client)

    assert 'not a page shown' in client.requests[1][-1]['content']
    assert len(_only(reading).pedigrees) == 1


def test_a_zoom_on_a_page_not_shown_is_refused_as_a_failed_call() -> None:
    client = _Client(_zoom([0, 0, 200, 150], page=5), _answer(_answer_set()))

    _read(client)

    result = client.requests[1][-1]['content'][0]
    assert result['is_error'] is True
    assert 'not a page shown' in result['content']


def _zoomed(page: int) -> str:
    """The image the model gets back for a zoom on `page` of a figure drawn on pages 1 and 2."""
    client = _Client(_zoom([0, 300, 600, 600], page=page), _answer(_answer_set(page=page)))

    _read(client, units=[_unit(0, 1, 2)])

    result = client.requests[1][-1]['content'][0]
    assert 'is_error' not in result
    return result['content'][0]['source']['data']


def test_a_zoom_renders_the_page_it_names() -> None:
    assert _zoomed(1) != _zoomed(2)


def test_a_legend_on_an_earlier_page_is_shown_after_the_figure_s_pages() -> None:
    client = _Client(_answer(_answer_set(page=2)))

    _read(client, units=[_unit(0, 2, 3, legend=1)])

    instruction = client.requests[0][0]['content'][-1]['text']
    order = [instruction.index(f'page {n} (') for n in (2, 3, 1)]
    assert order == sorted(order)
    assert instruction.index("the figure's legend") > order[2]


def test_a_figure_cannot_list_a_page_twice_or_its_legend_as_a_drawn_page() -> None:
    with pytest.raises(ValueError, match='distinct'):
        _unit(0, 1, 1)
    with pytest.raises(ValueError, match='legend'):
        _unit(0, 1, legend=1)
    with pytest.raises(ValueError, match='page order'):
        _unit(0, 2, 1)


def test_a_drawn_page_has_a_panel_on_it() -> None:
    (layout, *_) = pdf.page_layouts(_PDF).result()

    with pytest.raises(ValueError, match='at least one panel'):
        pedigree_extract.DrawnPage(layout, ())


def _paper(*units: pedigree_extract.FigurePages) -> pedigree_extract.PaperInput:
    return pedigree_extract.PaperInput(
        markdown=_MARKDOWN, pdf_bytes=_PDF, pdf_hash=_PDF_HASH, figures=units, variants=[]
    )


def test_a_paper_reads_each_figure_once_and_a_figure_is_an_index() -> None:
    with pytest.raises(ValueError, match='once'):
        _paper(_unit(0), _unit(0, 2))
    with pytest.raises(ValueError, match='index'):
        _unit(-1)


def test_a_paper_reads_no_figure_showing_more_pages_than_one_conversation_holds() -> None:
    (layout, *_) = pdf.page_layouts(_PDF).result()
    pages = [
        pedigree_extract.DrawnPage(dataclasses.replace(layout, number=n), [(0.1, 0.1, 0.9, 0.9)])
        for n in range(1, pedigree_extract.MAX_PAGES_SHOWN + 2)
    ]

    with pytest.raises(ValueError, match='past'):
        _paper(pedigree_extract.FigurePages(figure=0, label='Figure 1', pages=pages))


def test_a_figure_whose_reading_fails_says_why_and_the_others_are_kept() -> None:
    off_the_page = _answer_set()
    off_the_page['citations'][0]['region']['x1'] = 90_000
    rejected = [_answer(off_the_page)] * (pedigree_extract._REPAIRS + 1)  # pyright: ignore[reportPrivateUsage]
    client = _Client(*rejected, _answer(_answer_set(page=3)))

    reading = _read(client, units=[_unit(0, 1, 2), _unit(1, 3)])

    failed, kept = reading.construction.figures_read
    assert 'still rejected' in failed.failure
    assert not kept.HasField('failure')
    assert list(reading.pedigrees.figures) == [1]
    protovalidate.validate(reading.construction)


def test_a_turn_that_breaks_off_after_the_model_answered_is_counted_and_raised_as_spent() -> None:
    tokens = _Tokens()

    with pytest.raises(ocr.SpentError):
        _read(_Client(None), tokens=tokens)

    assert tokens.stops == ['unfinished']


def test_a_turn_stopping_for_a_tool_without_calling_one_is_read_as_the_answer() -> None:
    answer = _answer(_answer_set())
    answer.stop_reason = 'tool_use'
    client = _Client(answer)

    reading = _read(client)

    assert len(_only(reading).pedigrees) == 1
    assert len(client.requests) == 1


def test_an_empty_reply_is_not_sent_back() -> None:
    empty = _answer(_answer_set())
    empty.content = []
    client = _Client(empty, _answer(_answer_set()))

    _read(client)

    assert all(m['content'] for request in client.requests for m in request)


def test_a_legend_on_another_page_is_shown_after_the_page_and_the_figure_is_named() -> None:
    client = _Client(_answer(_answer_set()))

    _read(client, units=[_unit(0, 1, legend=2)])

    first_turn = client.requests[0][0]['content']
    assert [block['type'] for block in first_turn] == ['text', 'image', 'image', 'text']
    assert '"Figure 1"' in first_turn[-1]['text']


_GENE1 = pedigree_extract.KnownVariant(
    printed='c.123A>G', checked='NM_000001.2:c.123A>G', gene='GENE1', lines=('The proband carried c.123A>G.',)
)


def test_the_paper_variants_open_each_conversation_with_a_cache_point_of_their_own() -> None:
    client = _Client(_answer(_answer_set()), _answer(_answer_set()))

    _read(client, units=[_unit(0), _unit(1)], variants=[_GENE1])

    openings = [request[0]['content'][0] for request in client.requests if len(request) == 1]
    assert len(openings) == 2
    for opening in openings:
        assert 'NM_000001.2:c.123A>G' in opening['text']
        assert 'The proband carried c.123A>G.' in opening['text']
        assert _MARKDOWN not in opening['text']  # the variants and their lines, not the whole paper
        assert opening['cache_control'] == {'type': 'ephemeral'}
    assert openings[0]['text'] == openings[1]['text']  # one cached prefix for every figure of the paper


def test_a_paper_with_no_variants_says_so_before_the_page() -> None:
    client = _Client(_answer(_answer_set()))

    _read(client)

    opening = client.requests[0][0]['content'][0]
    assert opening['type'] == 'text'
    assert 'no checked variant' in opening['text']


def test_a_spend_limit_refuses_the_reading_rather_than_failing_its_figure() -> None:
    response = httpx2.Response(400, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    body = {'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified API usage limits'}}
    limited = anthropic.BadRequestError('usage limits', response=response, body=body)

    with pytest.raises(anthropic.BadRequestError):
        _read(_Client(limited))


def test_a_conversation_that_breaks_off_leaves_the_others_to_finish() -> None:
    client = _Client(None, _answer(_answer_set()))

    with pytest.raises(ocr.SpentError):
        _read(client, units=[_unit(0), _unit(1)])

    assert len(client.requests) == 2  # the second figure's turn ran to its end, so its usage was counted


def test_a_request_the_api_refuses_fails_its_figure_and_the_others_are_kept() -> None:
    response = httpx2.Response(413, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    too_large = anthropic.APIStatusError('request too large', response=response, body=None)
    client = _Client(_answer(_answer_set()), _zoom([0, 0, 200, 150]), too_large)

    reading = _read(client, units=[_unit(0), _unit(1)])

    first, second = reading.construction.figures_read
    assert not first.HasField('failure')
    failure = second.failure
    assert 'request too large' in failure
    assert len(_only(reading).pedigrees) == 1
