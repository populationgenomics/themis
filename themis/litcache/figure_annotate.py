"""Annotating a paper's figures: each pdf page shown to a small model on its own, which boxes the figures drawn on it.

One request per page (docs/design/figure-annotation.md): the page as an image the model reads unshrunk. The model
answers through one tool, whose input is every figure drawn on the page with its label, its legend's box when the
legend is on the page, and its panels, each with a box and every kind drawn in it; and every legend on the page. An
answer is accepted only when every box is a box on the page image; otherwise the reason goes back to the model to fix.
The pages' answers are then joined into the paper's figures: a figure drawn across pages becomes one figure, and a
figure whose legend is on another page is given it, by the rules in `assemble`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

import anthropic

from themis.litcache import anthropic_ocr, claude_images, claude_turns, ocr, page_render, pdf
from themis.litcache.models import figures_pb2

_LOG = logging.getLogger(__name__)

# A change to what the model does, or to how pages are joined, that `_harness_version` does not digest bumps this.
_REVISION = '2'
MODEL = 'claude-haiku-5-5'
_TIER = claude_images.HIGH_RESOLUTION
_MAX_TOKENS = 8_000
_REPAIRS = 2
# Pages a paper may have: a bound on what one paper spends, one request a page.
MAX_PAGES = 100
# Page requests of one paper in flight at once.
_CONCURRENCY = 8
# A box edge may overshoot its page image by this fraction of the image, and is brought back to the edge.
_OVERSHOOT = 0.02
# A box narrower or shorter than this, in page-image pixels, holds no panel.
_MIN_BOX = 4
_TIMEOUT_SECONDS = 600.0
# Rendering every page comes before the requests and their timeout.
_RENDERING_SECONDS = 180.0
LONGEST = datetime.timedelta(seconds=_RENDERING_SECONDS + _TIMEOUT_SECONDS)
# No byte from the API for this long ends a turn: the SDK's read timeout.
_STALL_SECONDS = 300.0
# A refusal the API repeats for the same request: malformed, too large, or unprocessable.
_REPEATED_STATUSES = frozenset({400, 413, 422})

_KIND_PREFIX = 'FIGURE_KIND_'
# The kinds the model chooses from, by their lower-case names, each to its enum value's name.
KINDS: dict[str, str] = {
    name.removeprefix(_KIND_PREFIX).lower(): name
    for name, value in figures_pb2.FigureKind.items()
    if value != figures_pb2.FIGURE_KIND_UNSPECIFIED
}

_BOX: dict[str, Any] = {
    'type': 'array',
    'items': {'type': 'number'},
    'minItems': 4,
    'maxItems': 4,
    'description': '[x0, y0, x1, y1] in pixels of the page image, top-left origin.',
}
TOOL: dict[str, Any] = {
    'name': 'figures',
    'description': 'Report every figure drawn on the page with its panels, and every legend on the page.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'figures': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'label': {
                            'type': 'string',
                            'description': 'As printed on this page, e.g. "Figure 2", "Fig. S1"; "" if none.',
                        },
                        'legend': {
                            'anyOf': [
                                {'type': 'object', 'properties': {'box': _BOX}, 'required': ['box']},
                                {'type': 'null'},
                            ],
                            'description': "The box of the figure's legend when it is on this page; null otherwise.",
                        },
                        'panels': {
                            'type': 'array',
                            'description': 'Empty when only the legend is on this page.',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'label': {
                                        'type': 'string',
                                        'description': 'The panel letter as printed; "" if none.',
                                    },
                                    'kinds': {
                                        'type': 'array',
                                        'minItems': 1,
                                        'items': {'type': 'string', 'enum': list(KINDS)},
                                    },
                                    'box': _BOX,
                                },
                                'required': ['label', 'kinds', 'box'],
                            },
                        },
                    },
                    'required': ['label', 'legend', 'panels'],
                },
            },
            'legends': {
                'type': 'array',
                'description': ('Every figure legend on this page, including one whose figure is on another page.'),
                'items': {
                    'type': 'object',
                    'properties': {
                        'label': {
                            'type': 'string',
                            'description': 'The figure label the legend begins with, as printed; "" if none.',
                        },
                        'box': _BOX,
                    },
                    'required': ['label', 'box'],
                },
            },
        },
        'required': ['figures', 'legends'],
    },
}
_TOOL_CHOICE: anthropic.types.ToolChoiceToolParam = {
    'type': 'tool',
    'name': TOOL['name'],
    'disable_parallel_tool_use': True,
}
_ASK = 'Annotate the figures on this page.'

SYSTEM = f"""\
You annotate the figures on one page of a scientific paper, shown as an image.

Report every figure drawn on this page, including supplementary figures and figures with no label. Not tables set
as text, logos, or equations.

For each figure:
- label: its label as printed on this page; "" if none is printed here, as on the continuation of a figure from
  another page. Never infer a label.
- legend: the box of its legend (caption) text if that is on this page; null otherwise.
- panels: each separately drawn sub-image on this page, lettered or not. A figure with one image is one panel;
  a figure whose legend is here but whose drawing is not has none. Each panel has:
  - box: [x0, y0, x1, y1] in absolute pixel coordinates of the page image, top-left origin, enclosing the panel's
    drawing and its own labels but not the legend.
  - kinds: every kind drawn in the panel, one or more of {', '.join(KINDS)}. A panel has a kind only when that
    kind of drawing is itself in it, not because its text mentions it. "pedigree" is a drawn family tree:
    individuals as squares, circles or diamonds, joined by lines between partners and lines down to their
    children, whatever the paper calls it. Person icons, the word "family", or a key of pedigree symbols alone
    are not a pedigree. A pedigree with a sequence trace under each person is "pedigree" and
    "sequence_chromatogram".

Also list every legend on this page under `legends`, with the label it begins with, including a legend whose
figure is on another page.

Report by calling the `{TOOL['name']}` tool once."""
# The instructions and tool repeat on every page of every paper: cached, unlike the page.
_SYSTEM_BLOCKS: list[anthropic.types.TextBlockParam] = [
    {'type': 'text', 'text': SYSTEM, 'cache_control': {'type': 'ephemeral'}}
]


def _repair(reason: str) -> str:
    return f'Rejected: {reason}. Call `{TOOL["name"]}` again with every figure and legend, corrected.'


def _harness_version() -> str:
    """The revision, and a digest of what the model is given and how its pages are rendered.

    A figure set made under any other version gives the step work for a run that names it.
    """
    given = {
        'model': MODEL,
        'max_tokens': _MAX_TOKENS,
        'tier': list(_TIER),
        'max_dpi': page_render.MAX_DPI,
        'repairs': _REPAIRS,
        'max_pages': MAX_PAGES,
        'overshoot': _OVERSHOOT,
        'min_box': _MIN_BOX,
        'system': SYSTEM,
        'tool': TOOL,
        'tool_choice': _TOOL_CHOICE,
        'ask': _ASK,
        'repair': _repair('{reason}'),
    }
    digest = hashlib.sha256(json.dumps(given, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f'{_REVISION}.{digest[:12]}'


HARNESS_VERSION = _harness_version()


class AnnotationError(Exception):
    """An annotation ended without a figure set it can record, as another attempt on the same pdf would.

    Attributes:
        refusal: For a refused request, what the API said of it.
    """

    def __init__(self, message: str, *, refusal: dict[str, str | None] | None = None) -> None:
        super().__init__(message)
        self.refusal = refusal


class _RejectedError(ValueError):
    """An answer the step will not take; the reason goes back to the model."""


@dataclasses.dataclass(frozen=True)
class Annotation:
    """A paper's figures, annotated.

    Attributes:
        figure_set: Every figure the model found, with its panels and legend placed on the pdf's pages.
        construction: How it was made, for the record.
        usage: Each turn's token usage, page by page.
    """

    figure_set: figures_pb2.FigureSet
    construction: figures_pb2.FigureAnnotationConstruction
    usage: list[anthropic.types.Usage]


@dataclasses.dataclass(frozen=True)
class Drawn:
    """One figure as one page draws it.

    Attributes:
        label: Its label as printed on the page; empty when the page prints none.
        legend: Its legend, when that is on the page.
        panels: Its panels on the page, placed.
    """

    label: str
    legend: figures_pb2.Place | None
    panels: list[figures_pb2.Panel]


@dataclasses.dataclass(frozen=True)
class PageAnswer:
    """One page's accepted answer.

    Attributes:
        number: The page's 1-based number.
        figures: The figures drawn on it, in the order the model reported them.
        legends: Each legend on it, as its printed label and its place.
    """

    number: int
    figures: list[Drawn]
    legends: list[tuple[str, figures_pb2.Place]]


def _numbers(value: object) -> list[float] | None:
    if not isinstance(value, list):
        return None
    numbers = [float(v) for v in value if isinstance(v, int | float) and not isinstance(v, bool)]  # pyright: ignore[reportUnknownVariableType]
    return numbers if len(numbers) == len(value) else None  # pyright: ignore[reportUnknownArgumentType]


def _place(raw: object, number: int, image: page_render.Image, where: str) -> figures_pb2.Place:
    """A box in the page image's pixels, as a place on the page in fractions of it.

    Raises:
        _RejectedError: The box is not four numbers making a box on the page image.
    """
    box = _numbers(raw)
    if box is None or len(box) != 4:
        raise _RejectedError(f'{where}: a box is four numbers [x0, y0, x1, y1]; got {raw!r}')
    width, height = image.width, image.height
    x0, y0, x1, y1 = box
    slack_x, slack_y = width * _OVERSHOOT, height * _OVERSHOOT
    if (
        min(x0, x1) < -slack_x
        or max(x0, x1) > width + slack_x
        or min(y0, y1) < -slack_y
        or max(y0, y1) > height + slack_y
    ):
        raise _RejectedError(f'{where}: the box {box} is off the {width} x {height} page image')
    x0, x1 = max(0.0, x0), min(float(width), x1)
    y0, y1 = max(0.0, y0), min(float(height), y1)
    if x1 - x0 < _MIN_BOX or y1 - y0 < _MIN_BOX:
        raise _RejectedError(f'{where}: the box {box} is not a box of at least {_MIN_BOX} px')
    return figures_pb2.Place(
        pdf_page=number,
        box=figures_pb2.Box(left=x0 / width, top=y0 / height, right=x1 / width, bottom=y1 / height),
    )


def _kinds(raw: object, where: str) -> list[str]:
    """The panel's kinds, as enum value names, each once, in the order the model gave them.

    Raises:
        _RejectedError: They are not a non-empty list of the kinds offered.
    """
    if not isinstance(raw, list) or not raw:
        raise _RejectedError(f'{where}: kinds is a non-empty list of {", ".join(KINDS)}')
    kinds: list[str] = []
    for name in raw:  # pyright: ignore[reportUnknownVariableType]
        kind = KINDS.get(name) if isinstance(name, str) else None
        if kind is None:
            raise _RejectedError(f'{where}: {name!r} is not one of {", ".join(KINDS)}')
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def _box_of(legend: object) -> object:
    """A figure's legend as the tool gives it, an object with a box, as its box; anything else as itself, refused."""
    return legend.get('box') if isinstance(legend, dict) else legend  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _drawn(raw: object, number: int, image: page_render.Image, where: str) -> Drawn:
    """One figure of the answer, placed; with no panels when only its legend is on the page.

    Raises:
        _RejectedError: It is not a figure with a string label, a legend box or null, and panels each with kinds and
            a box; or it has neither a panel nor a legend.
    """
    if not isinstance(raw, dict):
        raise _RejectedError(f'{where}: a figure is an object with label, legend and panels; got {raw!r}')
    label = raw.get('label', '')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    panels = raw.get('panels')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    legend = raw.get('legend')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(label, str) or not isinstance(panels, list) or (not panels and legend is None):
        raise _RejectedError(f'{where}: a figure has a string label, and at least one panel or a legend')
    placed: list[figures_pb2.Panel] = []
    for j, panel in enumerate(panels):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        at = f'{where} panel {j}'
        if not isinstance(panel, dict):
            raise _RejectedError(f'{at}: a panel is an object with label, kinds and box')
        panel_label = panel.get('label', '')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(panel_label, str):
            raise _RejectedError(f'{at}: label is a string')
        placed.append(
            figures_pb2.Panel(
                label=panel_label,
                kinds=_kinds(panel.get('kinds'), at),  # pyright: ignore[reportUnknownMemberType]
                place=_place(panel.get('box'), number, image, at),  # pyright: ignore[reportUnknownMemberType]
            )
        )
    return Drawn(
        label=label,
        legend=None if legend is None else _place(_box_of(legend), number, image, f'{where} legend'),
        panels=placed,
    )


def _accept(answer: object, number: int, image: page_render.Image) -> PageAnswer:
    """The tool's input as the page's answer.

    Raises:
        _RejectedError: It is not figures and legends as the tool describes them.
    """
    if not isinstance(answer, dict):
        raise _RejectedError(f'the answer is {{"figures": [...], "legends": [...]}}; got a {type(answer).__name__}')
    figures = answer.get('figures')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(figures, list):
        raise _RejectedError(f'figures is a list; got {type(figures).__name__}')  # pyright: ignore[reportUnknownArgumentType]
    # Required, yet now and then left out for a page with none, and repairs do not bring it back: read as none.
    legends = answer.get('legends', [])  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(legends, list):
        raise _RejectedError(f'legends is a list; got {type(legends).__name__}')  # pyright: ignore[reportUnknownArgumentType]
    placed_legends: list[tuple[str, figures_pb2.Place]] = []
    for i, legend in enumerate(legends):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        label = legend.get('label', '') if isinstance(legend, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(label, str):
            raise _RejectedError(f'legend {i}: a legend is an object with a string label and a box')
        placed_legends.append((label, _place(legend.get('box'), number, image, f'legend {i}')))  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    drawn = [_drawn(f, number, image, f'figure {i}') for i, f in enumerate(figures)]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    # A figure with only its legend on the page is that legend.
    placed_legends += [(d.label, d.legend) for d in drawn if not d.panels and d.legend is not None]
    return PageAnswer(number=number, figures=[d for d in drawn if d.panels], legends=placed_legends)


_LABEL = re.compile(
    r'\b(supp(?:l(?:ementa(?:ry|l))?)?\.?\s+|extended\s+data\s+|e-?)?fig(?:ure)?s?\.?\s*(s?\d+)', re.IGNORECASE
)
# Two boxes on a page are one legend when their overlap covers this share of the smaller.
_SAME_LEGEND = 0.5


def figure_key(label: str) -> str | None:
    """What a printed label names, for matching across pages.

    "Fig. 2" and "FIGURE 2" are "2"; "Figure S2", "Supplementary Figure 2" and "Extended Data Fig. 2" are "s2"; None
    for a label naming no numbered figure.
    """
    match = _LABEL.search(label)
    if match is None:
        return None
    number = match[2].lower()
    return number if number.startswith('s') or match[1] is None else f's{number}'


@dataclasses.dataclass(frozen=True)
class _Legend:
    page: int
    label: str
    place: figures_pb2.Place


@dataclasses.dataclass(eq=False)
class _Joined:
    label: str
    legend: int | None  # an index into the paper's legends
    panels: list[figures_pb2.Panel]
    pages: set[int]


def _overlap(a: figures_pb2.Box, b: figures_pb2.Box) -> float:
    width = min(a.right, b.right) - max(a.left, b.left)
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min((a.right - a.left) * (a.bottom - a.top), (b.right - b.left) * (b.bottom - b.top))
    return width * height / smaller


def _one_legend(label: str, place: figures_pb2.Place, other: _Legend) -> bool:
    """Whether a listed legend is `other` drawn again: mostly the same box, and not naming another figure."""
    if _overlap(place.box, other.place.box) < _SAME_LEGEND:
        return False
    mine, theirs = figure_key(label), figure_key(other.label)
    return mine is None or theirs is None or mine == theirs


def _legends(pages: Sequence[PageAnswer]) -> tuple[list[_Legend], dict[int, int]]:
    """Every legend the pages report, once, and for each drawing reported with its legend, that legend's index.

    The model reports a figure's legend on the figure and again in the page's list, with boxes drawn twice by eye:
    on one page, boxes that mostly overlap and do not name different figures are one legend, labelled with whichever
    label is printed.
    """
    legends: list[_Legend] = []
    own: dict[int, int] = {}
    for page in pages:
        first = len(legends)
        for drawn in page.figures:
            if drawn.legend is not None:
                own[id(drawn)] = len(legends)
                legends.append(_Legend(page.number, drawn.label, drawn.legend))
        for label, place in page.legends:
            same = next((i for i in range(first, len(legends)) if _one_legend(label, place, legends[i])), None)
            if same is None:
                legends.append(_Legend(page.number, label, place))
            elif not legends[same].label:
                legends[same] = dataclasses.replace(legends[same], label=label)
    return legends, own


def _reserve(
    pages: Sequence[PageAnswer],
    legends: Sequence[_Legend],
    own: dict[int, int],
    drawn_keys: set[str],
    carried: set[str],
) -> dict[int, int]:
    """For each unlabelled drawing without a legend of its own, an unclaimed legend on its own page, if there is one.

    Done for the whole paper before any drawing looks next door, so a page's own legend is never taken by a neighbour.
    """
    claimed = set(own.values())
    reserved: dict[int, int] = {}
    for page in pages:
        free = [
            i
            for i, legend in enumerate(legends)
            if legend.page == page.number and i not in claimed and figure_key(legend.label) not in drawn_keys
        ]
        for drawn in page.figures:
            counted = figure_key(drawn.label) in carried
            if not counted and id(drawn) not in own and free:
                reserved[id(drawn)] = free.pop(0)
    return reserved


def _unlabelled(
    number: int,
    drawn: Drawn,
    *,
    legends: Sequence[_Legend],
    drawn_keys: set[str],
    claimed: set[int],
    before: _Joined | None,
) -> _Joined:
    """The figure an unlabelled drawing with no legend on its own page belongs to.

    An unclaimed legend on the page before or after, that no labelled drawing's label names, makes it a figure of its
    own. Otherwise it continues the figure before it, if there is one, and stands alone if not.
    """
    candidates = sorted(
        (i for i, legend in enumerate(legends) if abs(legend.page - number) == 1 and i not in claimed),
        key=lambda i: legends[i].page,
    )
    for i in candidates:
        if figure_key(legends[i].label) not in drawn_keys:
            claimed.add(i)
            return _Joined(legends[i].label, i, list(drawn.panels), {number})
    if before is not None:
        before.panels.extend(drawn.panels)
        before.pages.add(number)
        return before
    return _Joined('', None, list(drawn.panels), {number})


def _legend_by_label(key: str, figure: _Joined, legends: Sequence[_Legend]) -> int | None:
    """The legend a labelled figure takes when it has none of its own.

    One with its label on a page next to the figure's comes first, since a legend listed on the figure's own page is
    sometimes the model echoing the label; then one on its own pages; then the nearest.
    """
    matches = [i for i, legend in enumerate(legends) if figure_key(legend.label) == key]

    def distance(i: int) -> int:
        return min(abs(legends[i].page - page) for page in figure.pages)

    adjacent = [i for i in matches if distance(i) == 1]
    on_its_pages = [i for i in matches if distance(i) == 0]
    return (adjacent or on_its_pages or sorted(matches, key=distance) or [None])[0]


def assemble(pages: Sequence[PageAnswer]) -> figures_pb2.FigureSet:
    """Join the pages' answers into the paper's figures.

    A label counts only when some legend in the paper carries it: a page that shows only part of a figure can come
    back labelled with a guess. Drawings on neighbouring pages that share a counted label are one figure, its panels
    in page order; the same label far from the figure's pages starts another, since one of the two is a misreading. A
    labelled figure without a legend of its own takes the legend with its label: next to its pages first, then on
    them, then the nearest. An unlabelled drawing takes an unclaimed legend on its own page, then one on the page
    before or after, that no labelled drawing's label names; otherwise it continues the figure before it, the last
    on its page or else on the page before. A legend goes to one figure, except that figures sharing a label share it.

    Args:
        pages: Every page's answer, in page order.

    Returns:
        The figures, in the order their first panel appears.
    """
    legends, own = _legends(pages)
    carried = {k for legend in legends if (k := figure_key(legend.label)) is not None}
    # The labels some drawn figure counts: a legend carrying one is that figure's, never a neighbour's.
    drawn_keys = {k for page in pages for drawn in page.figures if (k := figure_key(drawn.label)) in carried}
    reserved = _reserve(pages, legends, own, drawn_keys, carried)
    claimed = set(own.values()) | set(reserved.values())
    by_key: dict[str, _Joined] = {}
    keyed: list[tuple[str, _Joined]] = []
    joined: list[_Joined] = []
    last_on: dict[int, _Joined] = {}
    for page in pages:
        for drawn in page.figures:
            key = figure_key(drawn.label)
            if key is not None and key in carried:
                figure = by_key.get(key)
                if figure is None or page.number - max(figure.pages) > 1:
                    figure = by_key[key] = _Joined(drawn.label, None, [], set())
                    keyed.append((key, figure))
                    joined.append(figure)
                figure.panels.extend(drawn.panels)
                figure.pages.add(page.number)
                if figure.legend is None and id(drawn) in own:
                    figure.legend = own[id(drawn)]
            elif (index := own.get(id(drawn), reserved.get(id(drawn)))) is not None:
                # A numbered label no legend carries is a guess; the legend's own label stands instead.
                label = legends[index].label or (drawn.label if figure_key(drawn.label) is None else '')
                figure = _Joined(label, index, list(drawn.panels), {page.number})
                joined.append(figure)
            else:
                # The figure before it in reading order: beside it on its page, or else at the end of the last page.
                before = last_on.get(page.number) or last_on.get(page.number - 1)
                figure = _unlabelled(
                    page.number,
                    drawn,
                    legends=legends,
                    drawn_keys=drawn_keys,
                    claimed=claimed,
                    before=before,
                )
                if figure is not before:
                    joined.append(figure)
            last_on[page.number] = figure
    for key, figure in keyed:
        if figure.legend is None:
            figure.legend = _legend_by_label(key, figure, legends)
    return figures_pb2.FigureSet(
        figures=[
            figures_pb2.Figure(
                label=f.label, legend=None if f.legend is None else legends[f.legend].place, panels=f.panels
            )
            for f in joined
        ]
    )


def _request(image: page_render.Image) -> list[anthropic.types.MessageParam]:
    return [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': f'Page image, {image.width} x {image.height} px'},
                image.block(),
                {'type': 'text', 'text': _ASK},
            ],
        }
    ]


async def _converse(
    number: int, image: page_render.Image, *, client: anthropic.AsyncAnthropic, tokens: ocr.TokenCounter, model: str
) -> tuple[PageAnswer, list[anthropic.types.Usage], str]:
    """One page's request, repairing a rejected answer in the same conversation.

    Returns:
        The page's answer, each turn's usage, and the model the API reported.

    Raises:
        AnnotationError: The model refused or was cut off, the API refused the request as one it would refuse again,
            or the answer was still rejected after its repairs.
        ocr.SpentError: A turn failed after the model began answering it.
    """
    messages = _request(image)
    usage: list[anthropic.types.Usage] = []
    for repairs in range(_REPAIRS + 1):
        try:
            message, request_id = await claude_turns.stream(
                client,
                tokens=tokens,
                model=model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_BLOCKS,
                tools=[TOOL],
                tool_choice=_TOOL_CHOICE,
                messages=messages,
                # Only a repair sends the page again, and repairs are too rare to pay for writing every request.
                cached=False,
            )
        except anthropic.APIStatusError as e:
            if e.status_code not in _REPEATED_STATUSES or anthropic_ocr.spend_refused(e):
                raise
            raise AnnotationError(f'page {number}: the API refused the request: {e}') from e
        usage.append(message.usage)
        calls = [block for block in message.content if block.type == 'tool_use']
        if message.stop_reason != 'tool_use' or not calls:
            raise AnnotationError(
                f'page {number}: the annotation stopped with {message.stop_reason}',
                refusal=claude_turns.refusal(message, request_id) if message.stop_reason == 'refusal' else None,
            )
        try:
            if len(calls) > 1:
                raise _RejectedError(f'the answer is one `{TOOL["name"]}` call; got {len(calls)}')
            return _accept(calls[0].input, number, image), usage, message.model
        except _RejectedError as e:
            if repairs == _REPAIRS:
                raise AnnotationError(f'page {number}: an answer was still rejected: {e}') from e
            messages.append({'role': 'assistant', 'content': message.content})
            messages.append(
                {
                    'role': 'user',
                    'content': [
                        {'type': 'tool_result', 'tool_use_id': call.id, 'content': _repair(str(e)), 'is_error': True}
                        for call in calls
                    ],
                }
            )
    raise AssertionError('unreachable: the last repair raises')


def _worst(failures: Sequence[BaseException]) -> BaseException:
    """The failure that decides the paper's.

    One another attempt would repeat comes first, so a paper that cannot succeed is not paid for again; then one paid
    for; then the rest.
    """
    for kind in (AnnotationError, ocr.SpentError):
        for failure in failures:
            if isinstance(failure, kind):
                return failure
    return failures[0]


async def _annotate_pages(
    shown: dict[int, page_render.Image], *, client: anthropic.AsyncAnthropic, tokens: ocr.TokenCounter, model: str
) -> Annotation:
    """Every page's request, at most `_CONCURRENCY` at once, joined into the paper's figure set.

    Every page's request runs to its end even when another fails: a request cancelled before its response begins
    can be billed with no usage to count.

    Raises:
        AnnotationError: A page's request ended as `_converse` says; it decides the paper over any other failure.
        ocr.SpentError: A page's turn failed after the model began answering it.
    """
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(number: int, image: page_render.Image) -> tuple[PageAnswer, list[anthropic.types.Usage], str]:
        async with gate:
            return await _converse(number, image, client=client, tokens=tokens, model=model)

    outcomes = await asyncio.gather(*(one(n, image) for n, image in sorted(shown.items())), return_exceptions=True)
    failures = [o for o in outcomes if isinstance(o, BaseException)]
    if failures:
        _LOG.warning('%d of %d pages failed', len(failures), len(outcomes))
        raise _worst(failures)
    answers = [o for o in outcomes if not isinstance(o, BaseException)]
    figure_set = assemble([answer for answer, _, _ in answers])
    _LOG.info(
        'annotated %d figures, %d panels, on %d pages',
        len(figure_set.figures),
        sum(len(f.panels) for f in figure_set.figures),
        len(shown),
    )
    construction = figures_pb2.FigureAnnotationConstruction(model=answers[-1][2], harness_version=HARNESS_VERSION)
    return Annotation(
        figure_set=figure_set, construction=construction, usage=[u for _, usage, _ in answers for u in usage]
    )


async def _rendered(pdf_bytes: bytes, layouts: Sequence[pdf.PageLayout]) -> dict[int, page_render.Image]:
    """Every page rendered for its request, by page number.

    Raises:
        AnnotationError: The pdf has more than `MAX_PAGES` pages, or a page cannot be rendered or renders too large.
        pypdfium2.PdfiumError: pdfium crashed rendering a page; it would on every attempt.
        ValueError: The pdf has no pages.
    """
    if not layouts:
        raise ValueError('a pdf with no pages has no figures to annotate')
    if len(layouts) > MAX_PAGES:
        raise AnnotationError(f'the pdf has {len(layouts)} pages; at most {MAX_PAGES} are annotated')
    shown: dict[int, page_render.Image] = {}
    for layout in layouts:
        try:
            image = await page_render.render(pdf_bytes, layout, (0.0, 0.0, 1.0, 1.0), tier=_TIER)
        except ValueError as e:
            raise AnnotationError(f'page {layout.number} could not be rendered: {e}') from e
        if image.wire_bytes > claude_images.MAX_IMAGE_BASE64_BYTES:
            raise AnnotationError(f'page {layout.number} renders too large to send')
        shown[layout.number] = image
    return shown


async def annotate(
    pdf_bytes: bytes,
    layouts: Sequence[pdf.PageLayout],
    *,
    client: anthropic.AsyncAnthropic,
    tokens: ocr.TokenCounter,
    model: str = MODEL,
) -> Annotation:
    """Annotate every figure of a pdf, one request a page, repairing a rejected answer in the same conversation.

    Args:
        pdf_bytes: The pdf.
        layouts: Every page's layout, in page order.
        client: The Anthropic client the requests run on.
        tokens: Where each turn's usage is counted, whatever the outcome.
        model: The model to run.

    Returns:
        The paper's figure set, and how it was made.

    Raises:
        AnnotationError: The pdf has more than `MAX_PAGES` pages or a page renders too large, or a page's request
            ended as another attempt would end too: refused, cut off, refused by the API as malformed, or still
            rejected after its repairs.
        ocr.SpentError: A turn failed after the model began answering it; its usage so far is counted.
        pypdfium2.PdfiumError: pdfium crashed rendering a page.
        ValueError: The pdf has no pages.
    """
    shown = await _rendered(pdf_bytes, layouts)
    return await _annotate_pages(shown, client=client, tokens=tokens, model=model)


async def annotate_paper(
    pdf_bytes: bytes,
    layouts: Sequence[pdf.PageLayout],
    *,
    tokens: ocr.TokenCounter,
    credentials: anthropic_ocr.CredentialsFactory | None = None,
) -> Annotation:
    """The worker's annotation: every page, on a client of its own, within `_TIMEOUT_SECONDS`.

    Args:
        pdf_bytes: The pdf.
        layouts: Every page's layout, in page order.
        tokens: Where each turn's usage is counted.
        credentials: Builds the access-token provider for the call; None leaves the SDK to resolve credentials from
            the environment.

    Returns:
        The paper's figure set, and how it was made.

    Raises:
        AnnotationError: As `annotate`.
        ocr.SpendRefusedError: The API refused a first turn at a spend limit, or for billing; nothing was spent.
        ocr.SpentError: The annotation failed after a turn was paid for, or ran past `_TIMEOUT_SECONDS` after one.
            The original error is its cause.
        pypdfium2.PdfiumError: pdfium crashed rendering a page, before any request; it would on every attempt.
        anthropic.APIError: A first turn failed before the model answered; nothing was spent.
        TimeoutError: The annotation ran past `_TIMEOUT_SECONDS` before the model answered; nothing was spent.
    """
    # Rendering spends nothing and is bounded by pdfium's own process, so the requests' time is the model's alone.
    shown = await _rendered(pdf_bytes, layouts)
    billed = claude_turns.Billed(tokens)
    try:
        async with (
            anthropic.AsyncAnthropic(
                credentials=credentials() if credentials is not None else None, timeout=_STALL_SECONDS
            ) as client,
            asyncio.timeout(_TIMEOUT_SECONDS),
        ):
            return await _annotate_pages(shown, client=client, tokens=billed, model=MODEL)
    except (AnnotationError, ocr.SpentError):
        raise
    except Exception as e:  # noqa: BLE001 — classified and re-raised, never swallowed
        claude_turns.raise_classified(e, billed)
