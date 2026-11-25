"""The pedigree reader's conversations: one per figure, its pages and zooms, the checks an answer must pass."""

from __future__ import annotations

import asyncio
import base64
import collections
import dataclasses
import io
import json
import pathlib
import re
from collections.abc import Mapping, Sequence

import anthropic
import jsonschema
import PIL.Image
import protovalidate
import pytest
from grus import ir
from grus.models import pedigree_pb2

from themis.litcache import claude_images, ocr, pdf, pedigree_extract, pedigree_schema
from themis.testing import anthropic_sse

_PDF = (pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'oa' / 'source.pdf').read_bytes()
_PDF_HASH = 'c' * 64
_MARKDOWN = 'Results.\n**Figure 1** Pedigree of the family with retinitis pigmentosa, inherited as X-linked.\n'
_FIGURE_LABEL_RE = re.compile(r'Transcribe the pedigrees of "Figure (\d+)"')


def _answer_set(*, page: int = 1) -> dict[str, object]:
    entry: dict[str, object] = {'id': 'k1', 'text': 'retinitis pigmentosa', 'phenotype': {}}
    supports: list[dict[str, object]] = [{'field': 'key.text', 'key_id': 'k1', 'citations': ['c2']}]
    citations: list[dict[str, object]] = [
        {'id': 'c1', 'region': {'page': page, 'x0': 10, 'y0': 20, 'x1': 200, 'y1': 150}},
        {'id': 'c2', 'quote': 'retinitis pigmentosa'},
    ]
    return {
        'key_entries': [entry],
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
                        'marks': [{'key_id': 'k1', 'status': 'CONDITION_STATUS_AFFECTED'}],
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


def _turn(
    stop_reason: str | None,
    content: Sequence[anthropic_sse.Block] = (),
    *,
    request_id: str = 'req',
) -> anthropic_sse.MessageTurn:
    return anthropic_sse.MessageTurn(
        stop_reason=stop_reason,
        content=content,
        model=pedigree_extract.MODEL,
        request_id=request_id,
    )


def _answer(answer: Mapping[str, object], *, stop_reason: str = 'end_turn') -> anthropic_sse.MessageTurn:
    return _turn(stop_reason, [anthropic_sse.Text(json.dumps(answer))])


def _zoom(region: list[float], *, page: int = 1) -> anthropic_sse.MessageTurn:
    return _turn('tool_use', [anthropic_sse.ToolUse(id='z1', name='zoom', input={'page': page, 'region': region})])


def _figure_index(request: anthropic_sse.RecordedRequest) -> int:
    instruction = request.content_blocks(0)[-1]['text']
    assert isinstance(instruction, str)
    match = _FIGURE_LABEL_RE.search(instruction)
    assert match is not None
    return int(match.group(1)) - 1


def _by_figure(scripts: Mapping[int, Sequence[anthropic_sse.Turn]]) -> anthropic_sse.Harness:
    queues = {figure: collections.deque(turns) for figure, turns in scripts.items()}

    def respond(request: anthropic_sse.RecordedRequest) -> anthropic_sse.Turn:
        return queues[_figure_index(request)].popleft()

    return anthropic_sse.Harness(responder=respond)


class _Tokens:
    def __init__(self) -> None:
        self.stops: list[str] = []

    def add(self, usage: anthropic.types.Usage, *, model: str, stop_reason: str) -> None:
        del usage, model
        self.stops.append(stop_reason)


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
    harness: anthropic_sse.Harness,
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

    async def run() -> pedigree_extract.Reading:
        async with harness.client() as client:
            return await pedigree_extract.read(paper, client=client, tokens=tokens or _Tokens())

    return asyncio.run(run())


def _overview_size(harness: anthropic_sse.Harness) -> tuple[int, int]:
    source = harness.requests[0].content_blocks(0)[1]['source']
    assert isinstance(source, dict)
    data = source['data']
    assert isinstance(data, str)
    with PIL.Image.open(io.BytesIO(base64.standard_b64decode(data))) as image:
        return image.size


def test_a_page_read_with_a_zoom_is_placed_in_points_and_stamped_with_its_pdf() -> None:
    harness = anthropic_sse.Harness([_zoom([0, 0, 200, 150]), _answer(_answer_set())])

    reading = _read(harness)

    zoom_result = harness.requests[1].content_blocks(-1)[0]
    result_blocks = zoom_result['content']
    assert isinstance(result_blocks, list)
    assert isinstance(result_blocks[0], dict)
    assert result_blocks[0]['type'] == 'image'
    layout = pdf.page_layouts(_PDF).result()[0]
    width, height = _overview_size(harness)
    region, quote = _only(reading).citations
    assert {region.document_id, quote.document_id} == {_PDF_HASH}
    assert region.region.page == 0
    assert region.region.x1 == pytest.approx(200 / width * layout.width, rel=1e-4)
    assert region.region.y1 == pytest.approx(150 / height * layout.height, rel=1e-4)
    assert [(r.figure, r.HasField('failure')) for r in reading.construction.figures_read] == [(0, False)]


def test_the_overview_reaches_the_model_at_the_size_it_reads_it() -> None:
    harness = anthropic_sse.Harness([_answer({'pedigrees': []})])

    _read(harness)

    width, height = _overview_size(harness)
    tier = claude_images.HIGH_RESOLUTION
    assert claude_images.resized_size(width, height, max_edge=tier.max_edge, max_tokens=tier.max_tokens) == (
        width,
        height,
    )


def test_a_quote_the_paper_does_not_hold_goes_back_to_the_model_and_its_fix_is_kept() -> None:
    invented = _answer_set()
    citations = invented['citations']
    assert isinstance(citations, list)
    quote_citation = citations[1]
    assert isinstance(quote_citation, dict)
    quote_citation['quote'] = 'autosomal recessive inheritance'
    harness = anthropic_sse.Harness([_answer(invented), _answer(_answer_set())])

    reading = _read(harness)

    repair_prompt = harness.requests[1].messages[-1]['content']
    assert isinstance(repair_prompt, str)
    assert 'not in the paper' in repair_prompt
    assert _only(reading).citations[1].quote == 'retinitis pigmentosa'


def test_a_zoom_too_small_to_render_goes_back_to_the_model_as_a_failed_call() -> None:
    harness = anthropic_sse.Harness([_zoom([100, 100, 100.02, 100.02]), _answer(_answer_set())])

    reading = _read(harness)

    result = harness.requests[1].content_blocks(-1)[0]
    assert result['is_error'] is True
    assert len(_only(reading).pedigrees) == 1


def test_a_reading_where_every_figure_fails_ends_without_a_record() -> None:
    harness = anthropic_sse.Harness([_turn('refusal', request_id='req')])

    with pytest.raises(pedigree_extract.ExtractionError) as raised:
        _read(harness)

    assert raised.value.refusal is not None
    assert raised.value.refusal['request_id'] == 'req'


def _genotyped(answer: dict[str, object], *, allele: str = 'c.123A>G') -> dict[str, object]:
    """The answer with its proband genotyped heterozygous at one locus, as the label beside them shows."""
    answer['loci'] = [
        {
            'id': 'l1',
            'name': 'c.123A>G',
            'gene': 'GENE1',
            'alleles': [{'symbol': '+', 'reference': True}, {'symbol': 'M', 'name': allele}],
        }
    ]
    pedigrees = answer['pedigrees']
    assert isinstance(pedigrees, list)
    first_ped = pedigrees[0]
    assert isinstance(first_ped, dict)
    individuals = first_ped['individuals']
    assert isinstance(individuals, list)
    proband = individuals[2]
    assert isinstance(proband, dict)
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
    harness = _by_figure(
        {
            0: [_answer(_genotyped(_answer_set()))],
            1: [_answer(_genotyped(_answer_set(), allele='c.800del'))],
        }
    )

    reading = _read(harness, units=[_unit(0), _unit(1)])

    assert sorted(reading.pedigrees.figures) == [0, 1]
    names = []
    for index in (0, 1):
        one = reading.pedigrees.figures[index]
        (locus,) = one.loci
        names += [a.name for a in locus.alleles if a.symbol == 'M']
        assert len(_proband(one.pedigrees[0]).genotypes) == 1
        ir.validate_set(one)
    assert names == ['c.123A>G', 'c.800del']


def _every_kind_of_key_entry() -> dict[str, object]:
    """An answer whose key has a graded phenotype, a genotype at the proband's locus and an other entry, each shown."""
    answer = _genotyped(_answer_set())
    answer['key_entries'] = [
        {'id': 'k1', 'text': 'retinitis pigmentosa', 'phenotype': {'grades': ['suspected, not examined']}},
        {'id': 'k2', 'text': 'c.123A>G heterozygous', 'genotype': {'locus_id': 'l1', 'alleles': ['M', '+']}},
        {'id': 'k3', 'text': 'examined', 'other': {}},
    ]
    pedigrees = answer['pedigrees']
    assert isinstance(pedigrees, list)
    individuals = pedigrees[0]['individuals']
    individuals[1]['marks'] = [{'key_id': 'k1', 'grade': 'suspected, not examined'}]
    individuals[2]['marks'] = [
        {'key_id': 'k1', 'status': 'CONDITION_STATUS_AFFECTED'},
        {'key_id': 'k3'},
    ]
    return answer


def test_an_answer_with_every_kind_of_key_entry_meets_the_schema_the_model_is_given() -> None:
    jsonschema.validate(_every_kind_of_key_entry(), pedigree_schema.pedigree_set_schema())


def test_a_graded_phenotype_a_genotype_entry_and_an_other_entry_are_kept_in_the_figure_s_set() -> None:
    reading = _read(anthropic_sse.Harness([_answer(_every_kind_of_key_entry())]))

    one = _only(reading)
    kinds = {e.text: e.WhichOneof('denotes') for e in one.key_entries}
    assert kinds == {'retinitis pigmentosa': 'phenotype', 'c.123A>G heterozygous': 'genotype', 'examined': 'other'}
    mother = next(i for i in one.pedigrees[0].individuals if (i.generation, i.index) == (1, 2))
    assert [(m.key_id, m.grade) for m in mother.marks] == [('k1', 'suspected, not examined')]
    assert sorted(m.key_id for m in _proband(one.pedigrees[0]).marks) == ['k1', 'k3']


def test_a_figure_read_with_no_pedigree_on_it_is_recorded_with_an_empty_set() -> None:
    reading = _read(anthropic_sse.Harness([_answer({'pedigrees': []})]))

    assert list(reading.pedigrees.figures) == [0]
    assert not reading.pedigrees.figures[0].pedigrees


def test_a_figure_on_two_pages_is_read_in_one_conversation_that_shows_both() -> None:
    harness = anthropic_sse.Harness([_answer(_answer_set(page=2))])

    reading = _read(harness, units=[_unit(0, 1, 2)])

    (request,) = harness.requests
    first_turn = request.content_blocks(0)
    assert [block['type'] for block in first_turn] == ['text', 'image', 'image', 'text']
    instruction = first_turn[-1]['text']
    assert isinstance(instruction, str)
    assert 'page 1 (' in instruction
    assert 'page 2 (' in instruction
    region = next(c.region for c in _only(reading).citations if c.HasField('region'))
    assert region.page == 1  # grus counts pages from 0


def test_a_region_on_a_page_not_shown_goes_back_to_the_model() -> None:
    harness = anthropic_sse.Harness([_answer(_answer_set(page=3)), _answer(_answer_set())])

    reading = _read(harness)

    repair_prompt = harness.requests[1].messages[-1]['content']
    assert isinstance(repair_prompt, str)
    assert 'not a page shown' in repair_prompt
    assert len(_only(reading).pedigrees) == 1


def test_a_zoom_on_a_page_not_shown_is_refused_as_a_failed_call() -> None:
    harness = anthropic_sse.Harness([_zoom([0, 0, 200, 150], page=5), _answer(_answer_set())])

    _read(harness)

    result = harness.requests[1].content_blocks(-1)[0]
    assert result['is_error'] is True
    assert isinstance(result['content'], str)
    assert 'not a page shown' in result['content']


def _zoomed(page: int) -> str:
    """The image the model gets back for a zoom on `page` of a figure drawn on pages 1 and 2."""
    harness = anthropic_sse.Harness([_zoom([0, 300, 600, 600], page=page), _answer(_answer_set(page=page))])

    _read(harness, units=[_unit(0, 1, 2)])

    result = harness.requests[1].content_blocks(-1)[0]
    assert 'is_error' not in result
    content = result['content']
    assert isinstance(content, list)
    first_block = content[0]
    assert isinstance(first_block, dict)
    source = first_block['source']
    assert isinstance(source, dict)
    data = source['data']
    assert isinstance(data, str)
    return data


def test_a_zoom_renders_the_page_it_names() -> None:
    assert _zoomed(1) != _zoomed(2)


def test_a_legend_on_an_earlier_page_is_shown_after_the_figure_s_pages() -> None:
    harness = anthropic_sse.Harness([_answer(_answer_set(page=2))])

    _read(harness, units=[_unit(0, 2, 3, legend=1)])

    instruction = harness.requests[0].content_blocks(0)[-1]['text']
    assert isinstance(instruction, str)
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
    citations = off_the_page['citations']
    assert isinstance(citations, list)
    region_citation = citations[0]
    assert isinstance(region_citation, dict)
    region = region_citation['region']
    assert isinstance(region, dict)
    region['x1'] = 90_000
    rejected = [_answer(off_the_page)] * 3
    harness = _by_figure({0: rejected, 1: [_answer(_answer_set(page=3))]})

    reading = _read(harness, units=[_unit(0, 1, 2), _unit(1, 3)])

    failed, kept = reading.construction.figures_read
    assert 'still rejected' in failed.failure
    assert not kept.HasField('failure')
    assert list(reading.pedigrees.figures) == [1]
    protovalidate.validate(reading.construction)


def test_a_turn_that_breaks_off_after_the_model_answered_is_counted_and_raised_as_spent() -> None:
    tokens = _Tokens()
    harness = anthropic_sse.Harness([anthropic_sse.DroppedStream(model=pedigree_extract.MODEL)])

    with pytest.raises(ocr.SpentError):
        _read(harness, tokens=tokens)

    assert tokens.stops == ['unfinished']


def test_a_turn_stopping_for_a_tool_without_calling_one_is_read_as_the_answer() -> None:
    harness = anthropic_sse.Harness([_answer(_answer_set(), stop_reason='tool_use')])

    reading = _read(harness)

    assert len(_only(reading).pedigrees) == 1
    assert len(harness.requests) == 1


def test_an_empty_reply_is_not_sent_back() -> None:
    empty = _turn('end_turn', ())
    harness = anthropic_sse.Harness([empty, _answer(_answer_set())])

    _read(harness)

    assert all(m['content'] for request in harness.requests for m in request.messages)


_GENE1 = pedigree_extract.KnownVariant(
    printed='c.123A>G', checked='NM_000001.2:c.123A>G', gene='GENE1', lines=('The proband carried c.123A>G.',)
)


def test_the_paper_variants_open_each_conversation_with_a_cache_point_of_their_own() -> None:
    harness = _by_figure({0: [_answer(_answer_set())], 1: [_answer(_answer_set())]})

    _read(harness, units=[_unit(0), _unit(1)], variants=[_GENE1])

    openings = [request.content_blocks(0)[0] for request in harness.requests if len(request.messages) == 1]
    assert len(openings) == 2
    for opening in openings:
        text = opening['text']
        assert isinstance(text, str)
        assert 'NM_000001.2:c.123A>G' in text
        assert 'The proband carried c.123A>G.' in text
        assert _MARKDOWN not in text  # the variants and their lines, not the whole paper
        assert opening['cache_control'] == {'type': 'ephemeral'}
    assert openings[0]['text'] == openings[1]['text']  # one cached prefix for every figure of the paper


def test_a_spend_limit_refuses_the_reading_rather_than_failing_its_figure() -> None:
    limited = anthropic_sse.StatusError(
        status_code=400,
        error_type='invalid_request_error',
        message='You have reached your specified API usage limits',
    )

    with pytest.raises(anthropic.BadRequestError):
        _read(anthropic_sse.Harness([limited]))


def test_a_conversation_that_breaks_off_leaves_the_others_to_finish() -> None:
    harness = _by_figure(
        {
            0: [anthropic_sse.DroppedStream(model=pedigree_extract.MODEL)],
            1: [_answer(_answer_set())],
        }
    )

    with pytest.raises(ocr.SpentError):
        _read(harness, units=[_unit(0), _unit(1)])

    assert len(harness.requests) == 2  # the second figure's turn ran to its end, so its usage was counted


def test_a_request_the_api_refuses_fails_its_figure_and_the_others_are_kept() -> None:
    too_large = anthropic_sse.StatusError(
        status_code=413,
        error_type='request_too_large',
        message='request too large',
    )
    harness = _by_figure(
        {
            0: [_answer(_answer_set())],
            1: [_zoom([0, 0, 200, 150]), too_large],
        }
    )

    reading = _read(harness, units=[_unit(0), _unit(1)])

    first, second = reading.construction.figures_read
    assert not first.HasField('failure')
    assert 'request too large' in second.failure
    assert len(_only(reading).pedigrees) == 1
