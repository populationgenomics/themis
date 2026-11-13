"""The pedigree reader's conversations: zooms, the checks an answer must pass, failed figures, and the merged set."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import pathlib
import types
from collections.abc import Sequence
from typing import Any, Self, cast

import anthropic
import httpx2
import PIL.Image
import pytest
from grus import ir
from grus.models import pedigree_pb2

from themis.litcache import claude_images, ocr, pdf, pedigree_extract

_PDF = (pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'oa' / 'source.pdf').read_bytes()
_PDF_HASH = 'c' * 64
_MARKDOWN = 'Results.\n**Figure 1** Pedigree of the family with retinitis pigmentosa, inherited as X-linked.\n'


def _answer_set(*, inheritance: str | None = None) -> dict[str, Any]:
    condition: dict[str, Any] = {'id': 'k1', 'name': 'retinitis pigmentosa'}
    supports: list[dict[str, Any]] = [{'field': 'condition.name', 'condition_id': 'k1', 'citations': ['c2']}]
    citations: list[dict[str, Any]] = [
        {'id': 'c1', 'region': {'x0': 10, 'y0': 20, 'x1': 200, 'y1': 150}},
        {'id': 'c2', 'quote': 'retinitis pigmentosa'},
    ]
    if inheritance is not None:
        condition['inheritance'] = inheritance
        supports.append({'field': 'condition.inheritance', 'condition_id': 'k1', 'citations': ['c3']})
        citations.append({'id': 'c3', 'quote': 'X-linked'})
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


def _zoom(region: list[float]) -> anthropic.types.Message:
    call = anthropic.types.ToolUseBlock(type='tool_use', id='z1', name='zoom', input={'region': region})
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
def _one_page_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # The scripted client answers turns in order, so pages are read one after another.
    monkeypatch.setattr(pedigree_extract, '_CONCURRENCY', 1)


def _unit(figure: int, page: int, *, legend: int | None = None) -> pedigree_extract.FigurePage:
    layouts = pdf.page_layouts(_PDF).result()
    return pedigree_extract.FigurePage(
        figure=figure,
        label=f'Figure {figure + 1}',
        page=layouts[page - 1],
        boxes=[(0.1, 0.1, 0.9, 0.5)],
        legend=None if legend is None else layouts[legend - 1],
    )


def _read(
    client: _Client,
    *,
    units: list[pedigree_extract.FigurePage] | None = None,
    tokens: _Tokens | None = None,
    variants: Sequence[pedigree_extract.KnownVariant] = (),
) -> pedigree_extract.Reading:
    paper = pedigree_extract.PaperInput(
        markdown=_MARKDOWN,
        pdf_bytes=_PDF,
        pdf_hash=_PDF_HASH,
        figures=units or [_unit(0, 1)],
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
    region, quote = reading.pedigree_set.citations
    assert {region.document_id, quote.document_id} == {_PDF_HASH}
    assert region.region.page == 0
    assert region.region.x1 == pytest.approx(200 / width * layout.width, rel=1e-4)
    assert region.region.y1 == pytest.approx(150 / height * layout.height, rel=1e-4)
    assert [(r.figure, list(r.failures)) for r in reading.construction.figures_read] == [(0, [])]


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
    assert reading.pedigree_set.citations[1].quote == 'retinitis pigmentosa'


def test_a_zoom_too_small_to_render_goes_back_to_the_model_as_a_failed_call() -> None:
    client = _Client(_zoom([100, 100, 100.02, 100.02]), _answer(_answer_set()))

    reading = _read(client)

    result = client.requests[1][-1]['content'][0]
    assert result['is_error'] is True
    assert len(reading.pedigree_set.pedigrees) == 1


def test_a_figure_whose_answer_is_never_accepted_is_recorded_and_the_other_figures_are_kept() -> None:
    off_the_page = _answer_set()
    off_the_page['citations'][0]['region']['x1'] = 90_000
    rejected = [_answer(off_the_page)] * (pedigree_extract._REPAIRS + 1)  # pyright: ignore[reportPrivateUsage]
    client = _Client(*rejected, _answer(_answer_set()))

    reading = _read(client, units=[_unit(0, 1), _unit(1, 2)])

    first, second = reading.construction.figures_read
    assert first.failures
    assert not second.failures
    assert len(reading.pedigree_set.pedigrees) == 1


def test_a_reading_where_every_figure_fails_ends_without_a_record() -> None:
    client = _Client(_message('refusal', []))

    with pytest.raises(pedigree_extract.ExtractionError) as raised:
        _read(client)

    assert raised.value.refusal is not None
    assert raised.value.refusal['request_id'] == 'req'


def _genotyped(
    answer: dict[str, Any],
    *,
    locus: str = 'c.123A>G',
    allele: str = 'c.123A>G',
    wild: str = '+',
    gene: str | None = 'GENE1',
    supported: bool = False,
) -> dict[str, Any]:
    """The answer with its proband genotyped heterozygous at one locus, as the label beside them shows."""
    declared: dict[str, Any] = {
        'id': 'l1',
        'name': locus,
        'alleles': [{'symbol': wild, 'reference': True}, {'symbol': 'M', 'name': allele}],
    }
    if gene is not None:
        declared['gene'] = gene
    answer['loci'] = [declared]
    proband = answer['pedigrees'][0]['individuals'][2]
    proband['genotypes'] = [{'locus_id': 'l1', 'alleles': ['M', wild]}]
    if supported:
        answer['pedigrees'][0]['supports'].append(
            {
                'field': 'genotype.alleles',
                'loci': ['l1'],
                'individuals': [{'generation': 2, 'index': 1}],
                'citations': ['c2'],
            }
        )
    proband['annotations'] = [{'text': f'M/{wild}', 'type': 'ANNOTATION_TYPE_GENOTYPE', 'loci': ['l1']}]
    return answer


def _proband_answer(answer: dict[str, Any]) -> dict[str, Any]:
    return answer['pedigrees'][0]['individuals'][2]


def _proband(pedigree: pedigree_pb2.Pedigree) -> pedigree_pb2.Individual:
    return next(i for i in pedigree.individuals if i.proband)


def test_figures_genotyped_at_one_locus_merge_to_one_declaration() -> None:
    client = _Client(_answer(_genotyped(_answer_set())), _answer(_genotyped(_answer_set())))

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    (locus,) = merged.loci
    for pedigree in merged.pedigrees:
        proband = _proband(pedigree)
        assert [g.locus_id for g in proband.genotypes] == [locus.id]
        assert list(proband.annotations[0].loci) == [locus.id]
    ir.validate_set(merged)


def test_a_figure_giving_an_allele_another_meaning_keeps_its_label_but_not_its_genotype() -> None:
    client = _Client(
        _answer(_genotyped(_answer_set())),
        _answer(_genotyped(_answer_set(), allele='c.800del')),
    )

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    (locus,) = merged.loci
    assert [a.name for a in locus.alleles if a.symbol == 'M'] == ['c.123A>G']
    first, second = (_proband(p) for p in merged.pedigrees)
    assert len(first.genotypes) == 1
    assert not second.genotypes
    assert second.annotations[0].text == 'M/+'
    assert not second.annotations[0].loci
    ir.validate_set(merged)


def test_figures_writing_the_wild_type_differently_share_one_reference_allele() -> None:
    client = _Client(_answer(_genotyped(_answer_set())), _answer(_genotyped(_answer_set(), wild='WT')))

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    (locus,) = merged.loci
    assert [a.symbol for a in locus.alleles if a.reference] == ['+']
    assert [list(_proband(p).genotypes[0].alleles) for p in merged.pedigrees] == [['M', '+'], ['M', '+']]
    assert _proband(merged.pedigrees[1]).annotations[0].text == 'M/WT'
    ir.validate_set(merged)


def test_an_allele_one_figure_leaves_unnamed_takes_the_name_another_states() -> None:
    client = _Client(_answer(_genotyped(_answer_set(), allele='')), _answer(_genotyped(_answer_set())))

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    (locus,) = merged.loci
    assert [a.name for a in locus.alleles if a.symbol == 'M'] == ['c.123A>G']
    assert [len(_proband(p).genotypes) for p in merged.pedigrees] == [1, 1]
    ir.validate_set(merged)


def test_a_locus_one_figure_gives_no_gene_is_the_one_another_names_the_gene_of() -> None:
    client = _Client(_answer(_genotyped(_answer_set(), gene=None)), _answer(_genotyped(_answer_set())))

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    (locus,) = merged.loci
    assert locus.gene == 'GENE1'
    assert [len(_proband(p).genotypes) for p in merged.pedigrees] == [1, 1]
    ir.validate_set(merged)


def test_a_contradicting_figure_loses_the_supports_of_the_genotypes_it_loses() -> None:
    client = _Client(
        _answer(_genotyped(_answer_set())),
        _answer(_genotyped(_answer_set(), allele='c.800del', supported=True)),
    )

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    assert all(s.field != 'genotype.alleles' for s in merged.pedigrees[1].supports)
    ir.validate_set(merged)


def test_a_figure_whose_two_alleles_would_be_one_held_allele_loses_its_genotype_there() -> None:
    # The second figure's unnamed "M" is the first's M by symbol, and its "M2" is the first's M by name.
    second = _genotyped(_answer_set())
    second['loci'][0]['alleles'] = [
        {'symbol': '+', 'reference': True},
        {'symbol': 'M'},
        {'symbol': 'M2', 'name': 'c.123A>G'},
    ]
    _proband_answer(second)['genotypes'] = [{'locus_id': 'l1', 'alleles': ['M', 'M2']}]
    client = _Client(_answer(_genotyped(_answer_set())), _answer(second))

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    (locus,) = merged.loci
    assert sorted(a.symbol for a in locus.alleles) == ['+', 'M']
    assert [len(_proband(p).genotypes) for p in merged.pedigrees] == [1, 0]
    ir.validate_set(merged)


def test_one_figure_s_two_loci_never_merge_into_one_held_locus() -> None:
    # The second figure names "variant" twice, once with its gene; both match the first figure's gene-less locus.
    second = _genotyped(_answer_set(), locus='variant')
    second['loci'].append(
        {'id': 'l2', 'name': 'variant', 'alleles': [{'symbol': '+', 'reference': True}, {'symbol': 'N'}]}
    )
    _proband_answer(second)['genotypes'].append({'locus_id': 'l2', 'alleles': ['N', '+']})
    client = _Client(_answer(_genotyped(_answer_set(), locus='variant', gene=None)), _answer(second))

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    second_proband = _proband(merged.pedigrees[1])
    assert len({g.locus_id for g in second_proband.genotypes}) == len(second_proband.genotypes) == 2
    ir.validate_set(merged)


def test_unnamed_loci_stating_different_alleles_stay_two() -> None:
    client = _Client(
        _answer(_genotyped(_answer_set(), locus='')),
        _answer(_genotyped(_answer_set(), locus='', allele='c.800del')),
    )

    merged = _read(client, units=[_unit(0, 1), _unit(1, 2)]).pedigree_set

    assert len({locus.id for locus in merged.loci}) == 2
    assert [len(_proband(p).genotypes) for p in merged.pedigrees] == [1, 1]
    ir.validate_set(merged)


def test_figures_that_disagree_on_a_mode_of_inheritance_merge_to_one_declaration_stating_none() -> None:
    client = _Client(
        _answer(_answer_set(inheritance='INHERITANCE_X_LINKED_RECESSIVE')),
        _answer(_answer_set(inheritance='INHERITANCE_AUTOSOMAL_RECESSIVE')),
    )

    reading = _read(client, units=[_unit(0, 1), _unit(1, 2)])

    merged = reading.pedigree_set
    (declaration,) = merged.conditions
    assert not declaration.HasField('inheritance')
    assert all(s.field != 'condition.inheritance' for p in merged.pedigrees for s in p.supports)
    ir.validate_set(merged)


def test_a_mode_one_figure_states_holds_for_a_figure_that_does_not() -> None:
    client = _Client(_answer(_answer_set()), _answer(_answer_set(inheritance='INHERITANCE_X_LINKED_RECESSIVE')))

    reading = _read(client, units=[_unit(0, 1), _unit(1, 2)])

    (declaration,) = reading.pedigree_set.conditions
    assert declaration.inheritance == pedigree_pb2.INHERITANCE_X_LINKED_RECESSIVE
    assert len({c.id for c in reading.pedigree_set.citations}) == len(reading.pedigree_set.citations)


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

    assert len(reading.pedigree_set.pedigrees) == 1
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

    _read(client, units=[_unit(0, 1), _unit(1, 2)], variants=[_GENE1])

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


def test_a_figure_drawn_on_two_pages_is_one_entry_carrying_the_failed_page() -> None:
    off_the_page = _answer_set()
    off_the_page['citations'][0]['region']['x1'] = 90_000
    rejected = [_answer(off_the_page)] * (pedigree_extract._REPAIRS + 1)  # pyright: ignore[reportPrivateUsage]
    client = _Client(_answer(_answer_set()), *rejected)

    reading = _read(client, units=[_unit(0, 1), _unit(0, 2)])

    (entry,) = reading.construction.figures_read
    assert entry.figure == 0
    assert [f.pdf_page for f in entry.failures] == [2]
    assert len(reading.pedigree_set.pedigrees) == 1


def test_a_spend_limit_refuses_the_reading_rather_than_failing_its_figure() -> None:
    response = httpx2.Response(400, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    body = {'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified API usage limits'}}
    limited = anthropic.BadRequestError('usage limits', response=response, body=body)

    with pytest.raises(anthropic.BadRequestError):
        _read(_Client(limited))


def test_a_conversation_that_breaks_off_leaves_the_others_to_finish() -> None:
    client = _Client(None, _answer(_answer_set()))

    with pytest.raises(ocr.SpentError):
        _read(client, units=[_unit(0, 1), _unit(1, 2)])

    assert len(client.requests) == 2  # the second figure's turn ran to its end, so its usage was counted


def test_a_request_the_api_refuses_fails_its_figure_and_the_others_are_kept() -> None:
    response = httpx2.Response(413, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    too_large = anthropic.APIStatusError('request too large', response=response, body=None)
    client = _Client(_answer(_answer_set()), _zoom([0, 0, 200, 150]), too_large)

    reading = _read(client, units=[_unit(0, 1), _unit(1, 2)])

    first, second = reading.construction.figures_read
    assert not first.failures
    (failure,) = second.failures
    assert 'request too large' in failure.reason
    assert len(reading.pedigree_set.pedigrees) == 1
