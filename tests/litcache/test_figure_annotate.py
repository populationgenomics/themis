"""The figure annotation: one request a page, placing the model's boxes, repairs, and joining pages into figures."""

from __future__ import annotations

import asyncio
import copy
import pathlib
import types
from collections.abc import Sequence
from typing import Any, Self, cast

import anthropic
import httpx2
import pytest

from themis.litcache import figure_annotate, ocr, pdf
from themis.litcache.models import figures_pb2

_PDF = (pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'oa' / 'source.pdf').read_bytes()


def _message(stop_reason: str, content: list[Any]) -> anthropic.types.Message:
    return anthropic.types.Message(
        id='msg',
        type='message',
        role='assistant',
        model=figure_annotate.MODEL,
        content=content,
        stop_reason=stop_reason,  # pyright: ignore[reportArgumentType]
        stop_sequence=None,
        usage=anthropic.types.Usage(input_tokens=1000, output_tokens=10),
    )


def _call(figures: list[Any], legends: list[Any] | None = None) -> anthropic.types.Message:
    answer: dict[str, object] = {'figures': figures, 'legends': legends or []}
    return _message('tool_use', [anthropic.types.ToolUseBlock(type='tool_use', id='f1', name='figures', input=answer)])


_EMPTY = _call([])


class _Stream:
    request_id = 'req'

    def __init__(self, message: anthropic.types.Message) -> None:
        self._message = message
        self.current_message_snapshot = message
        self._sent = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> types.SimpleNamespace:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return types.SimpleNamespace(type='message_start')

    async def get_final_message(self) -> anthropic.types.Message:
        return self._message


class _Client:
    """Answers each page's turns from a script, a page with none scripted finding nothing; keeps each request.

    Pages run one at a time in the tests, so a request opening a conversation opens the next page's.
    """

    def __init__(self, turns: dict[int, Sequence[anthropic.types.Message]] | None = None) -> None:
        self.messages = self
        self._turns = {page: list(script) for page, script in (turns or {}).items()}
        self._page = 0
        self.requests: list[dict[str, Any]] = []

    def stream(self, **request: Any) -> _Stream:  # noqa: ANN401 — the SDK's keyword surface
        if len(request['messages']) == 1:
            self._page += 1
        self.requests.append(copy.deepcopy(request | {'page': self._page}))
        script = self._turns.get(self._page)
        return _Stream(script.pop(0) if script else _EMPTY)


class _Tokens:
    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        del usage, model, stop_reason


@pytest.fixture(autouse=True)
def _one_page_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(figure_annotate, '_CONCURRENCY', 1)


def _layouts(pages: int = 3) -> list[pdf.PageLayout]:
    return pdf.page_layouts(_PDF).result()[:pages]


def _annotate(client: _Client, pages: int = 3) -> figure_annotate.Annotation:
    return asyncio.run(
        figure_annotate.annotate(
            _PDF,
            _layouts(pages),
            client=cast(anthropic.AsyncAnthropic, client),
            tokens=cast(ocr.TokenCounter, _Tokens()),
        )
    )


def _image_size(client: _Client, page: int) -> tuple[int, int]:
    (request,) = [r for r in client.requests if r['page'] == page and len(r['messages']) == 1]
    header = request['messages'][0]['content'][0]['text']
    width, _, height = header.partition(', ')[2].removesuffix(' px').partition(' x ')
    return int(width), int(height)


def _figure(
    label: str = 'Figure 1',
    *,
    box: list[float] | None = None,
    kinds: list[str] | None = None,
    legend: list[float] | None = None,
) -> dict[str, Any]:
    panel = {'label': 'A', 'kinds': ['pedigree'] if kinds is None else kinds, 'box': box or [100, 200, 600, 700]}
    return {'label': label, 'legend': None if legend is None else {'box': legend}, 'panels': [panel]}


def test_each_page_is_its_own_request_and_each_box_is_placed_on_that_page() -> None:
    client = _Client({2: [_call([_figure(legend=[50, 800, 900, 860], kinds=['pedigree', 'sequence_chromatogram'])])]})

    annotation = _annotate(client)

    assert [r['page'] for r in client.requests] == [1, 2, 3]
    for request in client.requests:
        assert [b['type'] for b in request['messages'][0]['content']].count('image') == 1
        assert request['tool_choice']['name'] == 'figures'
        assert request['tool_choice']['disable_parallel_tool_use'] is True
    width, height = _image_size(client, 2)
    (figure,) = annotation.figure_set.figures
    (panel,) = figure.panels
    assert list(panel.kinds) == [figures_pb2.FIGURE_KIND_PEDIGREE, figures_pb2.FIGURE_KIND_SEQUENCE_CHROMATOGRAM]
    assert panel.place.pdf_page == 2
    assert panel.place.box.right == pytest.approx(600 / width)
    assert panel.place.box.bottom == pytest.approx(700 / height)
    assert figure.legend.pdf_page == 2
    assert annotation.construction.harness_version == figure_annotate.HARNESS_VERSION


def test_the_instructions_are_cached_and_the_page_is_not() -> None:
    client = _Client()

    _annotate(client, pages=1)

    (request,) = client.requests
    assert not isinstance(request.get('cache_control'), dict)
    (block,) = request['system']
    assert block['cache_control'] == {'type': 'ephemeral'}


def test_a_box_a_little_past_the_edge_is_brought_back_to_it() -> None:
    probe = _Client()
    _annotate(probe, pages=1)
    width, height = _image_size(probe, 1)
    client = _Client({1: [_call([_figure(box=[-3, 10, width + 5, height + 4])])]})

    (figure,) = _annotate(client, pages=1).figure_set.figures

    box = figure.panels[0].place.box
    assert (box.left, box.right, box.bottom) == (0, 1, 1)


@pytest.mark.parametrize(
    'bad',
    [
        _call([_figure(box=[600, 200, 100, 700])]),  # corners swapped
        _call([_figure(box=[100, 200, 102, 700])]),  # too narrow to hold a panel
        _call([_figure(box=[100, 200, 600, 90_000])]),  # off the page
        _call([_figure(kinds=['cartoon'])]),  # a kind not offered
        _call([_figure(kinds=[])]),  # no kind
        _call(['Figure 1']),  # not a figure
        _call([], legends=[{'label': 'Figure 1'}]),  # a legend with no box
    ],
)
def test_an_answer_no_reader_could_place_goes_back_and_its_fix_is_kept(bad: anthropic.types.Message) -> None:
    client = _Client({1: [bad, _call([_figure()])]})

    annotation = _annotate(client, pages=1)

    reply = client.requests[1]['messages'][-1]['content'][0]
    assert reply['type'] == 'tool_result'
    assert reply['is_error'] is True
    assert len(annotation.figure_set.figures) == 1


def test_a_page_with_no_legends_may_leave_the_list_out() -> None:
    no_list = _message(
        'tool_use',
        [anthropic.types.ToolUseBlock(type='tool_use', id='f1', name='figures', input={'figures': [_figure()]})],
    )
    client = _Client({1: [no_list]})

    (figure,) = _annotate(client, pages=1).figure_set.figures

    assert len(client.requests) == 1
    assert len(figure.panels) == 1


def test_a_figure_reported_with_only_its_legend_is_a_legend_on_the_page() -> None:
    legend_only = {'label': 'Figure 1', 'legend': {'box': [50, 100, 900, 300]}, 'panels': []}
    client = _Client({1: [_call([_figure()])], 2: [_call([legend_only])]})

    (figure,) = _annotate(client).figure_set.figures

    assert [p.place.pdf_page for p in figure.panels] == [1]
    assert figure.legend.pdf_page == 2
    assert len([r for r in client.requests if r['page'] == 2]) == 1  # taken, not sent back


def test_a_figure_with_neither_a_panel_nor_a_legend_goes_back() -> None:
    client = _Client({1: [_call([{'label': 'Figure 1', 'legend': None, 'panels': []}]), _call([])]})

    _annotate(client, pages=1)

    assert len(client.requests) == 2


def test_a_kind_named_twice_is_kept_once() -> None:
    client = _Client({1: [_call([_figure(kinds=['pedigree', 'pedigree'])])]})

    (figure,) = _annotate(client, pages=1).figure_set.figures

    assert list(figure.panels[0].kinds) == [figures_pb2.FIGURE_KIND_PEDIGREE]


def test_an_answer_still_rejected_after_its_repairs_ends_the_annotation() -> None:
    rejected = [_call([_figure(kinds=['cartoon'])])] * (figure_annotate._REPAIRS + 1)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(figure_annotate.AnnotationError, match='page 2: an answer was still rejected'):
        _annotate(_Client({2: rejected}))


def test_a_refused_page_ends_the_annotation_and_says_what_the_api_said() -> None:
    client = _Client({1: [_message('refusal', [])]})

    with pytest.raises(figure_annotate.AnnotationError) as raised:
        _annotate(client)

    assert raised.value.refusal is not None
    assert raised.value.refusal['request_id'] == 'req'
    assert [r['page'] for r in client.requests] == [1, 2, 3]  # every page's request still ran, so its usage counts


def test_a_pdf_past_the_page_limit_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(figure_annotate, 'MAX_PAGES', 1)
    client = _Client()

    with pytest.raises(figure_annotate.AnnotationError, match='pages'):
        _annotate(client)

    assert not client.requests


def test_the_model_is_offered_every_kind_the_record_holds() -> None:
    offered = set(figure_annotate.KINDS.values())

    assert offered == set(figures_pb2.FigureKind.keys()) - {'FIGURE_KIND_UNSPECIFIED'}


def test_an_answer_split_over_two_calls_goes_back_with_both_answered() -> None:
    split = _call([_figure()])
    split.content.append(
        anthropic.types.ToolUseBlock(
            type='tool_use', id='f2', name='figures', input={'figures': [_figure('Figure 2')], 'legends': []}
        )
    )
    both = _call([_figure(legend=[50, 800, 400, 860]), _figure('Figure 2', legend=[500, 800, 900, 860])])
    client = _Client({1: [split, both]})

    annotation = _annotate(client, pages=1)

    reply = client.requests[1]['messages'][-1]['content']
    assert [r['tool_use_id'] for r in reply] == ['f1', 'f2']
    assert len(annotation.figure_set.figures) == 2


class _Refusing:
    """A client whose every request the API refuses as malformed."""

    def __init__(self) -> None:
        self.messages = self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def stream(self, **_request: object) -> _Stream:
        response = httpx2.Response(400, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
        raise anthropic.BadRequestError('invalid_request_error: image too large', response=response, body=None)


def test_a_request_the_api_refuses_as_malformed_ends_the_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(figure_annotate.anthropic, 'AsyncAnthropic', lambda **_: _Refusing())

    with pytest.raises(figure_annotate.AnnotationError, match='refused the request'):
        asyncio.run(figure_annotate.annotate_paper(_PDF, _layouts(), tokens=cast(ocr.TokenCounter, _Tokens())))


# --- joining pages into figures


def _place(page: int, top: float = 0.1) -> figures_pb2.Place:
    return figures_pb2.Place(pdf_page=page, box=figures_pb2.Box(left=0.1, top=top, right=0.9, bottom=top + 0.2))


def _drawn(page: int, label: str = '', *, legend: bool = False) -> figure_annotate.Drawn:
    panel = figures_pb2.Panel(kinds=['FIGURE_KIND_PEDIGREE'], place=_place(page))
    return figure_annotate.Drawn(label=label, legend=_place(page, 0.7) if legend else None, panels=[panel])


def _page(number: int, *figures: figure_annotate.Drawn, legends: Sequence[str] = ()) -> figure_annotate.PageAnswer:
    # The first listed legend sits where `_drawn` puts a figure's own legend; the rest stack above it.
    tops = [0.7, 0.45, 0.2, 0.0]
    return figure_annotate.PageAnswer(
        number=number,
        figures=list(figures),
        legends=[(label, _place(number, top)) for label, top in zip(legends, tops, strict=False)],
    )


def _shape(figure_set: figures_pb2.FigureSet) -> list[tuple[str, list[int], int | None]]:
    """Each figure as its label, its panels' pages and its legend's page."""
    return [
        (f.label, [p.place.pdf_page for p in f.panels], f.legend.pdf_page if f.HasField('legend') else None)
        for f in figure_set.figures
    ]


@pytest.mark.parametrize(
    ('label', 'key'),
    [
        ('Figure 2', '2'),
        ('FIG. 2', '2'),
        ('Fig 2', '2'),
        ('Figure S2.', 's2'),
        ('Supplementary Figure 2', 's2'),
        ('E-Figure 1', 's1'),
        ('Supplemental Figure 2', 's2'),
        ('Supp. Fig. 2', 's2'),
        ('Suppl. Fig. 3', 's3'),
        ('Extended Data Fig. 2', 's2'),
        ('Scheme 1', None),
        ('', None),
    ],
)
def test_a_printed_label_names_its_figure_however_it_is_spelled(label: str, key: str | None) -> None:
    assert figure_annotate.figure_key(label) == key


def test_a_figure_with_its_legend_on_its_page_stands_alone() -> None:
    pages = [_page(1, _drawn(1, 'Figure 1', legend=True), legends=['Figure 1']), _page(2)]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [1], 1)]


def test_a_labelled_figure_takes_the_legend_with_its_label_from_the_next_page() -> None:
    pages = [_page(3, _drawn(3, 'Figure 1')), _page(4, legends=['FIGURE 1.'])]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [3], 4)]


def test_a_figure_drawn_across_pages_under_one_label_is_one_figure() -> None:
    pages = [_page(4, _drawn(4, 'Figure 1', legend=True)), _page(5, _drawn(5, 'Figure 1'))]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [4, 5], 4)]


def test_a_label_no_legend_carries_is_a_guess_and_the_page_continues_the_figure_before() -> None:
    pages = [_page(4, _drawn(4, 'Fig. 1', legend=True)), _page(5, _drawn(5, 'Figure 3'))]

    assert _shape(figure_annotate.assemble(pages)) == [('Fig. 1', [4, 5], 4)]


def test_a_guessed_label_yields_to_the_legend_its_drawing_takes_on_its_page() -> None:
    pages = [_page(1, _drawn(1, 'Figure 1', legend=True)), _page(2, _drawn(2, 'Figure 3'), legends=['Figure 2'])]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [1], 1), ('Figure 2', [2], 2)]


def test_an_unlabelled_figure_takes_an_unclaimed_legend_next_door() -> None:
    pages = [_page(1, _drawn(1, 'Figure 1', legend=True)), _page(2, _drawn(2)), _page(3, legends=['Figure 2'])]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [1], 1), ('Figure 2', [2], 3)]


def test_a_legend_on_another_page_wins_over_one_listed_on_the_figure_own_page() -> None:
    pages = [_page(3, _drawn(3, 'Figure 1'), legends=['FIGURE 1']), _page(4, legends=['Figure 1'])]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [3], 4)]


def test_a_figure_own_legend_listed_again_is_never_given_to_a_neighbour() -> None:
    pages = [
        _page(4, _drawn(4, legend=True), legends=['Figure 1']),
        _page(5, _drawn(5)),
        _page(6, legends=['Figure 2']),
    ]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [4], 4), ('Figure 2', [5], 6)]


def test_a_continuation_page_does_not_take_the_legend_of_the_drawing_on_the_next_page() -> None:
    pages = [
        _page(1, _drawn(1, 'Figure 1', legend=True)),
        _page(2, _drawn(2)),
        _page(3, _drawn(3), legends=['Figure 2']),
    ]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [1, 2], 1), ('Figure 2', [3], 3)]


def test_a_loose_legend_box_does_not_swallow_a_legend_naming_another_figure() -> None:
    wide = figures_pb2.Place(pdf_page=5, box=figures_pb2.Box(left=0.05, top=0.6, right=0.95, bottom=0.95))
    figure_2 = figure_annotate.Drawn(
        label='Figure 2', legend=wide, panels=[figures_pb2.Panel(kinds=['FIGURE_KIND_CHART'], place=_place(5))]
    )
    right_column = figures_pb2.Place(pdf_page=5, box=figures_pb2.Box(left=0.55, top=0.65, right=0.9, bottom=0.9))
    pages = [
        _page(4, _drawn(4, 'Figure 1')),
        figure_annotate.PageAnswer(number=5, figures=[figure_2], legends=[('Figure 1', right_column)]),
    ]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [4], 5), ('Figure 2', [5], 5)]


def test_a_labelled_figure_takes_the_legend_next_to_it_over_one_far_away() -> None:
    pages = [_page(3, _drawn(3, 'Figure 1')), _page(4, legends=['Figure 1']), _page(20, legends=['Figure 1'])]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [3], 4)]


def test_an_unlabelled_figure_takes_the_unclaimed_legend_on_its_own_page_first() -> None:
    pages = [_page(3, _drawn(3), legends=['Figure 3']), _page(4, legends=['Figure 4'])]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 3', [3], 3)]


def test_the_same_label_far_from_its_figure_starts_another_figure() -> None:
    pages = [_page(2, _drawn(2, 'Figure 2', legend=True)), _page(9, _drawn(9, 'Figure 2'))]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 2', [2], 2), ('Figure 2', [9], 2)]


def test_an_unlabelled_drawing_beside_a_figure_joins_it_rather_than_the_page_before() -> None:
    pages = [
        _page(4, _drawn(4, 'Fig. 1', legend=True)),
        _page(5, _drawn(5, 'Figure 2', legend=True), _drawn(5)),
    ]

    assert _shape(figure_annotate.assemble(pages)) == [('Fig. 1', [4], 4), ('Figure 2', [5, 5], 5)]


def test_a_figure_split_over_one_page_with_its_legend_on_the_page_before_stays_one_figure() -> None:
    pages = [
        _page(4, legends=['Figure 1']),
        _page(5, _drawn(5, 'Figure 1'), _drawn(5), _drawn(5)),
    ]

    assert _shape(figure_annotate.assemble(pages)) == [('Figure 1', [5, 5, 5], 4)]


def test_a_page_that_would_fail_again_decides_the_paper_over_one_whose_turn_broke_off() -> None:
    refused = figure_annotate.AnnotationError('page 2: refused')
    broke_off = ocr.SpentError('page 7: the stream broke off')

    assert figure_annotate._worst([broke_off, refused]) is refused  # pyright: ignore[reportPrivateUsage]


def test_supplementary_figures_match_their_legends_however_each_is_spelled() -> None:
    pages = [
        _page(8, _drawn(8, 'Figure 2', legend=True)),
        _page(15, legends=['Figure S1.', 'Figure S2.']),
        _page(17, _drawn(17, 'Supplementary Figure 1')),
        _page(18, _drawn(18, 'Supplementary Figure 2')),
    ]

    assert _shape(figure_annotate.assemble(pages)) == [
        ('Figure 2', [8], 8),
        ('Supplementary Figure 1', [17], 15),
        ('Supplementary Figure 2', [18], 15),
    ]


def test_an_unlabelled_figure_with_nothing_before_it_stands_alone_without_a_legend() -> None:
    pages = [_page(1, _drawn(1)), _page(2)]

    assert _shape(figure_annotate.assemble(pages)) == [('', [1], None)]
