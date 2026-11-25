"""Reading a paper's pedigrees: each pedigree figure shown to a model with a zoom tool, and the answers checked.

The paper's figure set says which figures hold a pedigree, on which pages, and where their legends are
(docs/design/pedigree-extraction.md). One conversation per figure, after grus's page extraction: the model sees every
page the figure draws pedigrees on, whole and sized to its image tier, and the legend's page after them when that is
another; asks for any region of a page shown at full resolution with `zoom`; and answers with a grus pedigree set as
JSON. The answer is accepted only when grus's loader accepts it, every cited region lies on a page shown, and every
quote is in the paper; otherwise the reason goes back to the model to fix. The paper's record maps each figure to its
set; a figure whose answer is never accepted is recorded with the reason, and the other figures are kept.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import importlib.metadata
import json
import logging
from collections.abc import Sequence
from typing import Any

import anthropic
import protovalidate
import pypdfium2
from google.protobuf import json_format
from grus import ir
from grus.models import pedigree_pb2

from themis.litcache import (
    anthropic_ocr,
    claude_images,
    claude_turns,
    ocr,
    page_render,
    pdf,
    pedigree_prompt,
    pedigree_schema,
    variant_scan,
)
from themis.litcache.models import paper_pedigrees_pb2, pedigrees_pb2

_LOG = logging.getLogger(__name__)

# A change to what the model does that `_harness_version` does not digest, such as how a tool words its result, bumps
# this.
_REVISION = '2'
MODEL = 'claude-sonnet-5-5'
# No effort is set: the API's default is what grus's reading ladder measured.
_EFFORT: str | None = None
_TIER = claude_images.HIGH_RESOLUTION
_MAX_TOKENS = 64_000
# The pages shown, the legend's included, and the zooms: the twenty images grus's ladder read with.
_MAX_IMAGES = 20
# Turns past the zoom budget before a conversation is given up: room for bad regions and the repairs.
_SLACK_TURNS = 6
_REPAIRS = 2
# Every turn re-sends the conversation's images; their base64 is kept under this, below the request's 128 MB ceiling
# with room for the prompt and the text.
_REQUEST_IMAGE_BYTES = 100 * 1024 * 1024
# A zoom region narrower or shorter than this, in page-image pixels, holds nothing to read.
_MIN_REGION = 2
# Conversations at once, within one paper.
_CONCURRENCY = 3
# The whole paper, inside the worker's 30-minute dispatch deadline with room for its reads and commit.
_TIMEOUT_SECONDS = 1500.0
LONGEST = datetime.timedelta(seconds=_TIMEOUT_SECONDS)
# No byte from the API for this long ends a turn: the SDK's read timeout.
_STALL_SECONDS = 300.0
# A refusal the API repeats for the same request: malformed, too large, or unprocessable.
_REPEATED_STATUSES = frozenset({400, 413, 422})

ZOOM_TOOL: dict[str, Any] = {
    'name': 'zoom',
    'description': (
        'Re-render a region of one of the pages shown at higher resolution, from the source document. `page` is '
        'the page number as the instruction names it; `region` is [x0, y0, x1, y1]: the top-left and bottom-right '
        "corners in pixel coordinates of that page's image, never a zoomed image's. Returns the region as an "
        'image, scaled to the largest size you accept.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'page': {'type': 'integer', 'description': 'The page number, as the instruction names it.'},
            'region': {
                'type': 'array',
                'items': {'type': 'number'},
                'minItems': 4,
                'maxItems': 4,
                'description': "[x0, y0, x1, y1] in pixel coordinates of that page's image.",
            },
        },
        'required': ['page', 'region'],
    },
}
# The pages one conversation shows before its zooms: a figure's pedigree pages and its legend's page. A figure over
# this is not read, leaving most of the image budget to zooms.
MAX_PAGES_SHOWN = 6
# Every page shown can be its largest and the opening request still fits.
assert MAX_PAGES_SHOWN * claude_images.MAX_IMAGE_BASE64_BYTES < _REQUEST_IMAGE_BYTES  # noqa: S101


def _system() -> str:
    citations = pedigree_prompt.PAGE_CITATION_ADDENDUM.format(fields=', '.join(sorted(ir.SUPPORT_FIELDS)))
    return (
        f'{pedigree_prompt.EXTRACTION_PROMPT}\n{pedigree_prompt.PAGE_ADDENDUM}\n{pedigree_prompt.PAGE_ZOOM_ADDENDUM}\n'
        f'{pedigree_prompt.GENOTYPE_ADDENDUM}\n{citations}\n\n'
        f'{pedigree_prompt.SCHEMA_GUIDE}{json.dumps(pedigree_schema.pedigree_set_schema())}'
    )


# Heads the paper's validated variants in a conversation's opening turn.
_VARIANTS_HEAD = (
    "The paper's variants, as Themis read them and checked each against its reference sequence: as printed, the "
    'checked name, the gene, and the lines of the paper that print it. A variant here is not stated to be carried by '
    'anyone in this family unless the figure or a line says so. The page images that follow are what you transcribe.\n'
)
_NO_VARIANTS = 'Themis found no checked variant in this paper. The page images that follow are what you transcribe.\n'
# Bounds on what one paper's list shows: a cohort's table of hundreds of variants is cut, not sent whole.
_MAX_VARIANTS = 150
_LINES_PER_VARIANT = 2
_MAX_LINE = 300


def _variants_text(variants: Sequence[KnownVariant]) -> str:
    """The opening turn's list of the paper's variants, each with the lines that print it."""
    if not variants:
        return _NO_VARIANTS
    rows = []
    for variant in variants[:_MAX_VARIANTS]:
        lines = '; '.join(f'"{line[:_MAX_LINE]}"' for line in variant.lines[:_LINES_PER_VARIANT])
        rows.append(f'- "{variant.printed}" = {variant.checked} ({variant.gene or "gene not stated"}): {lines}')
    return _VARIANTS_HEAD + '\n'.join(rows) + '\n'


def _instruction(
    figure: str,
    drawn: Sequence[tuple[int, page_render.Image, str]],
    legend: tuple[int, page_render.Image] | None,
    zooms: int,
) -> str:
    """The opening turn's instruction: each image's page number and size, and what of the figure is on it.

    `drawn` holds the figure's pages in the order shown: each page's number, its image, and the figure's pedigree
    panels on it as boxes in the image's pixels. `legend` is the legend's page, shown last, when that is another.
    """
    pages = [
        f'page {number} ({image.width} x {image.height} px): its pedigree panels at about {boxes}'
        for number, image, boxes in drawn
    ]
    if legend is not None:
        number, image = legend
        pages.append(f"page {number} ({image.width} x {image.height} px): the figure's legend")
    return (
        f'The images are pages of the paper, in this order: {"; ".join(pages)}. Boxes are in pixel coordinates of '
        f"their page's image. Transcribe the pedigrees of {figure} into the required JSON; any other figure on these "
        'pages is read separately. A family drawn across two pages is one pedigree. Report meaning only, never '
        f'geometry. You may call `zoom` up to {zooms} times, naming the page and a region in its image.'
    )


def _repair(error: Exception) -> str:
    return (
        f'Your JSON was rejected:\n{error}\n\nRe-read the pages (zoom again if you need to) and reply with the '
        'corrected JSON only.'
    )


def _harness_version() -> str:
    """The revision, and a digest of what the model is given and how its images are made.

    A pedigree set made under any other version gives the step work for a run that names it.
    """
    probe = page_render.Image(png=b'', width=0, height=0)
    given = {
        'model': MODEL,
        'effort': _EFFORT,
        'max_tokens': _MAX_TOKENS,
        'tier': list(_TIER),
        'max_dpi': page_render.MAX_DPI,
        'request_image_bytes': _REQUEST_IMAGE_BYTES,
        'max_images': _MAX_IMAGES,
        'slack_turns': _SLACK_TURNS,
        'min_region': _MIN_REGION,
        'repair': _repair(ValueError('{reason}')),
        'repairs': _REPAIRS,
        'system': _system(),
        'variants': [_VARIANTS_HEAD, _NO_VARIANTS, _MAX_VARIANTS, _LINES_PER_VARIANT, _MAX_LINE],
        'instruction': _instruction('Figure 0', [(1, probe, '[]')], (2, probe), 0),
        'max_pages_shown': MAX_PAGES_SHOWN,
        'tool': ZOOM_TOOL,
        'support_fields': sorted(ir.SUPPORT_FIELDS),
        'grus': importlib.metadata.version('grus'),
    }
    digest = hashlib.sha256(json.dumps(given, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f'{_REVISION}.{digest[:12]}'


HARNESS_VERSION = _harness_version()


class ExtractionError(Exception):
    """A reading ended with nothing to record: no figure's answer was accepted.

    Attributes:
        refusal: For a single refused conversation, what the API said of it.
    """

    def __init__(self, message: str, *, refusal: dict[str, str | None] | None = None) -> None:
        super().__init__(message)
        self.refusal = refusal


class _FailedError(Exception):
    """One conversation ended without an accepted answer; the paper's other figures stand."""

    def __init__(self, message: str, *, refusal: dict[str, str | None] | None = None) -> None:
        super().__init__(message)
        self.refusal = refusal


class _RejectedError(ValueError):
    """An answer or a zoom the step will not take; the reason goes back to the model."""


@dataclasses.dataclass(frozen=True)
class DrawnPage:
    """A page a pedigree figure is drawn on.

    Attributes:
        page: The page.
        boxes: The figure's pedigree panels on it, as fractions of the page from its top-left corner.
    """

    page: pdf.PageLayout
    boxes: Sequence[tuple[float, float, float, float]]

    def __post_init__(self) -> None:
        """Check the page has a panel on it.

        Raises:
            ValueError: `boxes` is empty.
        """
        if not self.boxes:
            raise ValueError(f'a figure is drawn on page {self.page.number} in at least one panel; got none')


@dataclasses.dataclass(frozen=True)
class FigurePages:
    """A pedigree figure, as one conversation reads it.

    Attributes:
        figure: The figure's index in the paper's figure set.
        label: The figure's label as printed, or empty.
        pages: Every page it draws pedigrees on, in page order.
        legend: The page carrying its legend, when that is none of `pages`.
    """

    figure: int
    label: str
    pages: Sequence[DrawnPage]
    legend: pdf.PageLayout | None = None

    def __post_init__(self) -> None:
        """Check the figure is an index, its pages are distinct and in order, and the legend's page is none of them.

        Raises:
            ValueError: The figure's index is negative, a page is listed twice or out of order, or the legend's page is
                a drawn page.
        """
        if self.figure < 0:
            raise ValueError(f'a figure is an index in the figure set; got {self.figure}')
        numbers = [drawn.page.number for drawn in self.pages]
        if not numbers or numbers != sorted(set(numbers)):
            raise ValueError(f'a figure is drawn on distinct pages in page order, at least one; got {numbers}')
        if self.legend is not None and self.legend.number in numbers:
            raise ValueError(f'the legend is on drawn page {self.legend.number}: leave `legend` unset')

    @property
    def shown(self) -> list[pdf.PageLayout]:
        """The pages its conversation shows, in order: the drawn pages, then the legend's when that is another."""
        return [drawn.page for drawn in self.pages] + ([] if self.legend is None else [self.legend])

    @property
    def name(self) -> str:
        """The figure as a log line names it: its label, or its index."""
        return self.label or f'figure {self.figure}'


@dataclasses.dataclass(frozen=True)
class KnownVariant:
    """One of the paper's variants, as the variant steps read and checked it.

    Attributes:
        printed: As the paper prints it.
        checked: The name it was checked under, against a reference sequence.
        gene: Its gene, as the paper states it; empty when it does not.
        lines: The lines of the paper's rendering that print it, in order.
    """

    printed: str
    checked: str
    gene: str
    lines: Sequence[str]


@dataclasses.dataclass(frozen=True)
class PaperInput:
    """What a paper's pedigrees are read from.

    Attributes:
        markdown: The paper's rendering; every quote must be in it or in the text layer of a page shown.
        pdf_bytes: The pdf.
        pdf_hash: The pdf revision's hash, stamped on every citation.
        figures: Its pedigree figures to read, in figure order.
        variants: Its validated variants, which the reader names alleles by; empty for a paper with none.
    """

    markdown: str
    pdf_bytes: bytes
    pdf_hash: str
    figures: Sequence[FigurePages]
    variants: Sequence[KnownVariant]

    def __post_init__(self) -> None:
        """Check each figure is listed once and shows no more than `MAX_PAGES_SHOWN` pages.

        Raises:
            ValueError: A figure is listed twice, or would show too many pages.
        """
        indices = [unit.figure for unit in self.figures]
        if len(set(indices)) != len(indices):
            raise ValueError(f'a figure is read once; got figures {indices}')
        for unit in self.figures:
            if len(unit.shown) > MAX_PAGES_SHOWN:
                raise ValueError(f'{unit.name} shows {len(unit.shown)} pages, past {MAX_PAGES_SHOWN}')


@dataclasses.dataclass(frozen=True)
class Reading:
    """A paper's pedigrees, read.

    Attributes:
        pedigrees: For each figure whose answer was accepted, the grus set read from it: its pedigrees, the
            key entries and loci its legend declares, and the citations that place them.
        construction: How it was made, for the record: every figure read, a failed one with the reason.
        usage: Each turn's token usage: each conversation's turns in order, the conversations in figure order.
    """

    pedigrees: paper_pedigrees_pb2.PaperPedigrees
    construction: pedigrees_pb2.PedigreeExtractionConstruction
    usage: list[anthropic.types.Usage]


def _overview_box(region: object, overview: page_render.Image) -> tuple[float, float, float, float]:
    """A box the model gave in page-image pixels, as fractions of the page.

    Raises:
        _RejectedError: It is not four numbers, or not a box of at least `_MIN_REGION` pixels inside the image.
    """
    numbers: list[float] = []
    if isinstance(region, list | tuple):
        for v in region:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(v, int | float) and not isinstance(v, bool):
                numbers.append(float(v))
    if len(numbers) != 4 or len(numbers) != len(region):  # pyright: ignore[reportArgumentType]
        raise _RejectedError(f'a region is four numbers [x0, y0, x1, y1]; got {region!r}')
    x0, y0, x1, y1 = numbers
    inside = x0 >= 0 and x1 <= overview.width and y0 >= 0 and y1 <= overview.height
    if not (inside and x1 - x0 >= _MIN_REGION and y1 - y0 >= _MIN_REGION):
        raise _RejectedError(
            f'the region {region!r} is not a box of at least {_MIN_REGION} px inside the '
            f'{overview.width} x {overview.height} page image'
        )
    return (x0 / overview.width, y0 / overview.height, x1 / overview.width, y1 / overview.height)


def _json_object(text: str) -> str:
    """The first balanced `{...}` in `text`, past any prose or fence around it; the text itself when there is none."""
    start = text.find('{')
    if start == -1:
        return text
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class _Conversation:
    """One figure's conversation: the pages it shows, the zooms it made, and the image bytes every turn re-sends."""

    def __init__(
        self,
        pdf_bytes: bytes,
        pdf_hash: str,
        unit: FigurePages,
        images: Sequence[page_render.Image],
        variants_text: str,
    ) -> None:
        self._pdf = pdf_bytes
        self._variants_text = variants_text
        self.pdf_hash = pdf_hash
        self.unit = unit
        self._layouts = {layout.number: layout for layout in unit.shown}
        self._images = dict(zip(self._layouts, images, strict=True))
        self.zoom_budget = _MAX_IMAGES - len(images)
        self.zooms = 0
        self.wire_bytes = sum(image.wire_bytes for image in images)

    @property
    def name(self) -> str:
        numbers = ', '.join(str(drawn.page.number) for drawn in self.unit.pages)
        return f'{self.unit.name} on page{"s" if len(self.unit.pages) > 1 else ""} {numbers}'

    def first(self) -> list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam]:
        """The opening turn: the paper's variants, the figure's pages and its legend's page, and the instruction.

        The variants come first, with a cache point of their own, so each figure of a paper reads them from the cache.
        """
        drawn: list[tuple[int, page_render.Image, str]] = []
        for page in self.unit.pages:
            image = self._images[page.page.number]
            w, h = image.width, image.height
            placed = ', '.join(
                f'[{round(left * w)}, {round(top * h)}, {round(right * w)}, {round(bottom * h)}]'
                for left, top, right, bottom in page.boxes
            )
            drawn.append((page.page.number, image, placed))
        legend = None if self.unit.legend is None else (self.unit.legend.number, self._images[self.unit.legend.number])
        figure = f'"{self.unit.label}"' if self.unit.label else 'the unlabelled figure'
        content: list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam] = [
            {'type': 'text', 'text': self._variants_text, 'cache_control': {'type': 'ephemeral'}},
            *(image.block() for image in self._images.values()),
        ]
        text = _instruction(figure, drawn, legend, self.zoom_budget)
        content.append({'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral'}})
        return content

    def _page(self, page: object) -> int:
        """The number of a page this conversation shows.

        Raises:
            _RejectedError: `page` is not one of them.
        """
        if isinstance(page, int) and not isinstance(page, bool) and page in self._images:
            return page
        shown = ', '.join(map(str, self._images))
        raise _RejectedError(f'page {page!r} is not a page shown; the pages are {shown}')

    async def zoom(self, block: anthropic.types.ToolUseBlock) -> anthropic.types.ToolResultBlockParam:
        """Answer one zoom call: the region as an image, or the reason it was refused."""

        def error(message: str) -> anthropic.types.ToolResultBlockParam:
            return {'type': 'tool_result', 'tool_use_id': block.id, 'content': message, 'is_error': True}

        if block.name != ZOOM_TOOL['name']:
            return error(f'unknown tool {block.name!r}; the only tool is `zoom`')
        if self.zooms >= self.zoom_budget:
            return error('the zoom budget is spent; reply with the JSON now')
        arguments = block.input if isinstance(block.input, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        region = arguments.get('region')  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        try:
            number = self._page(arguments.get('page'))  # pyright: ignore[reportUnknownMemberType]
            box = _overview_box(region, self._images[number])
            image = await page_render.render(self._pdf, self._layouts[number], box, tier=_TIER)
        except _RejectedError as e:
            return error(str(e))
        except (ValueError, pypdfium2.PdfiumError) as e:
            return error(f'that region could not be rendered ({e}); choose another')
        if image.wire_bytes > claude_images.MAX_IMAGE_BASE64_BYTES:
            return error('that region renders too large to send; zoom into a smaller part of it')
        if self.wire_bytes + image.wire_bytes > _REQUEST_IMAGE_BYTES:
            return error('the conversation holds as many images as it can; reply with the JSON now')
        self.zooms += 1
        self.wire_bytes += image.wire_bytes
        note = f'Region {region} of page {number}.'
        if image.native_limited:
            note += ' Native resolution reached: zooming further into this area adds no detail.'
        return {
            'type': 'tool_result',
            'tool_use_id': block.id,
            'content': [image.block(), {'type': 'text', 'text': note}],
        }

    def accept(self, text: str, paper_text: str) -> pedigree_pb2.PedigreeSet:
        """The answer as a pedigree set, its regions placed on their pages and its quotes found in the paper.

        Raises:
            _RejectedError: grus's loader refuses it, a region is not on a page shown, or a quote is not in the paper.
        """
        try:
            answer = ir.load_set_json(_json_object(text))
        except (json_format.ParseError, ir.ValidationError, ir.IntegrityError) as e:
            raise _RejectedError(str(e)) from e
        haystack = variant_scan.normalise(paper_text) + ''.join(
            variant_scan.normalise(layout.text) for layout in self._layouts.values()
        )
        for citation in answer.citations:
            citation.document_id = self.pdf_hash
            if citation.HasField('region'):
                r = citation.region
                try:
                    number = self._page(r.page)
                    left, top, right, bottom = _overview_box([r.x0, r.y0, r.x1, r.y1], self._images[number])
                except _RejectedError as e:
                    raise _RejectedError(f'citation {citation.id!r}: {e}') from e
                layout = self._layouts[number]
                r.page = number - 1  # grus counts pages from 0
                r.x0, r.x1 = left * layout.width, right * layout.width
                r.y0, r.y1 = top * layout.height, bottom * layout.height
                continue
            quote = variant_scan.normalise(citation.quote)
            if not quote or quote not in haystack:
                raise _RejectedError(
                    f'citation {citation.id!r}: the quote {citation.quote!r} is not in the paper. Cite the sentence '
                    'that states the fact, copied exactly, or drop the fact.'
                )
        return answer


async def _open(pdf_bytes: bytes, pdf_hash: str, unit: FigurePages, variants_text: str) -> _Conversation:
    """Render what a conversation shows: the figure's pages, then its legend's page when that is another.

    Raises:
        _FailedError: A page cannot be rendered, or renders too large to send.
    """
    images: list[page_render.Image] = []
    for layout in unit.shown:
        try:
            image = await page_render.render(pdf_bytes, layout, (0.0, 0.0, 1.0, 1.0), tier=_TIER)
        except (ValueError, pypdfium2.PdfiumError) as e:
            raise _FailedError(f'page {layout.number} could not be rendered: {e}') from e
        if image.wire_bytes > claude_images.MAX_IMAGE_BASE64_BYTES:
            raise _FailedError(f'page {layout.number} renders too large to send')
        images.append(image)
    return _Conversation(pdf_bytes, pdf_hash, unit, images, variants_text)


async def _read(
    client: anthropic.AsyncAnthropic,
    conversation: _Conversation,
    *,
    paper_text: str,
    tokens: ocr.TokenCounter,
    model: str,
    usage: list[anthropic.types.Usage],
) -> tuple[pedigree_pb2.PedigreeSet, str]:
    """One conversation, to an accepted pedigree set; with the model the API reported.

    Raises:
        _FailedError: The model refused or was cut off, an answer was still rejected after its repairs, or the turns
            ran out.
        ocr.SpentError: A turn failed after the model began answering it.
    """
    name = conversation.name
    messages: list[anthropic.types.MessageParam] = [{'role': 'user', 'content': conversation.first()}]
    system = _system()
    repairs = 0
    for _ in range(conversation.zoom_budget + _SLACK_TURNS):
        message, request_id = await claude_turns.stream(
            client,
            tokens=tokens,
            model=model,
            max_tokens=_MAX_TOKENS,
            system=system,
            tools=[ZOOM_TOOL],
            messages=messages,
            cached=True,
            effort=_EFFORT,
        )
        usage.append(message.usage)
        if message.content:  # the API refuses an empty turn sent back
            messages.append({'role': 'assistant', 'content': message.content})
        calls = [block for block in message.content if block.type == 'tool_use']
        if calls:
            messages.append({'role': 'user', 'content': [await conversation.zoom(call) for call in calls]})
            continue
        if message.stop_reason == 'pause_turn':
            continue
        # A stop for a tool with no tool call in the turn ends it as `end_turn` does: its text is the answer.
        if message.stop_reason not in ('end_turn', 'tool_use'):
            raise _FailedError(
                f'{name} stopped with {message.stop_reason}',
                refusal=claude_turns.refusal(message, request_id) if message.stop_reason == 'refusal' else None,
            )
        text = ''.join(block.text for block in message.content if block.type == 'text')
        try:
            return conversation.accept(text, paper_text), message.model
        except _RejectedError as e:
            if repairs == _REPAIRS:
                raise _FailedError(f'{name}: an answer was still rejected: {e}') from e
            repairs += 1
            messages.append({'role': 'user', 'content': _repair(e)})
    raise _FailedError(f'{name}: no answer after {conversation.zoom_budget + _SLACK_TURNS} turns')


def _first(failures: Sequence[BaseException]) -> BaseException:
    """The failure a reading reports for its conversations: a paid one before any other."""
    return next((e for e in failures if isinstance(e, ocr.SpentError)), failures[0])


def _construction(
    units: Sequence[FigurePages],
    outcomes: Sequence[tuple[pedigree_pb2.PedigreeSet, str] | _FailedError],
    model: str,
) -> pedigrees_pb2.PedigreeExtractionConstruction:
    """Every figure read, in order; one whose reading failed carries the reason."""
    figures = [
        pedigrees_pb2.PedigreeFigure(figure=unit.figure, failure=str(outcome))
        if isinstance(outcome, _FailedError)
        else pedigrees_pb2.PedigreeFigure(figure=unit.figure)
        for unit, outcome in zip(units, outcomes, strict=True)
    ]
    return pedigrees_pb2.PedigreeExtractionConstruction(
        model=model, harness_version=HARNESS_VERSION, figures_read=figures
    )


async def read(
    paper: PaperInput,
    *,
    client: anthropic.AsyncAnthropic,
    tokens: ocr.TokenCounter,
    model: str = MODEL,
) -> Reading:
    """Read every pedigree figure of a paper, one conversation each and a few at once, into a pedigree set per figure.

    A figure whose reading fails gets no set; the construction gives the reason.

    Args:
        paper: What to read, and the rendering quotes are checked against.
        client: The Anthropic client the conversations run on.
        tokens: Where each turn's usage is counted, whatever the outcome.
        model: The model to run.

    Returns:
        A pedigree set for each figure whose answer was accepted, and how they were made.

    Raises:
        ExtractionError: No figure's answer was accepted; another attempt is expected to end the same way.
        ocr.SpentError: A turn failed after the model began answering it; its usage so far is counted.
        ValueError: The paper has no figure to read.
    """
    if not paper.figures:
        raise ValueError('a paper with no pedigree figure has no pedigrees to read')
    usage: list[list[anthropic.types.Usage]] = [[] for _ in paper.figures]
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(index: int, unit: FigurePages) -> tuple[pedigree_pb2.PedigreeSet, str] | _FailedError:
        async with gate:
            try:
                conversation = await _open(paper.pdf_bytes, paper.pdf_hash, unit, _variants_text(paper.variants))
                return await _read(
                    client, conversation, paper_text=paper.markdown, tokens=tokens, model=model, usage=usage[index]
                )
            except _FailedError as e:
                _LOG.warning('%s', e)
                return e
            except anthropic.APIStatusError as e:
                if e.status_code not in _REPEATED_STATUSES or anthropic_ocr.spend_refused(e):
                    raise  # a spend limit clears when the budget does: the reading is refused, not failed
                # The same conversation makes the same request: the rest of the paper's figures stand.
                failed = _FailedError(f'{unit.name}: {e}')
                _LOG.warning('%s', failed)
                return failed

    # Every conversation runs to its end even when another fails: a turn cancelled before its response begins can be
    # billed with no usage to count.
    gathered = await asyncio.gather(*(one(i, unit) for i, unit in enumerate(paper.figures)), return_exceptions=True)
    clearable = [o for o in gathered if isinstance(o, BaseException) and not isinstance(o, _FailedError)]
    if clearable:
        # A conversation failed in a way a later attempt could clear, such as a paid turn that broke off.
        raise _first(clearable)
    outcomes = [o for o in gathered if not isinstance(o, BaseException) or isinstance(o, _FailedError)]
    accepted = [o for o in outcomes if not isinstance(o, _FailedError)]
    if not accepted:
        failures = [o for o in outcomes if isinstance(o, _FailedError)]
        refusal = failures[0].refusal if len(failures) == 1 else None
        raise ExtractionError('; '.join(str(f) for f in failures), refusal=refusal)
    pedigrees = paper_pedigrees_pb2.PaperPedigrees()
    for unit, outcome in zip(paper.figures, outcomes, strict=True):
        if not isinstance(outcome, _FailedError):
            pedigrees.figures[unit.figure].CopyFrom(outcome[0])
    protovalidate.validate(pedigrees)
    _LOG.info(
        'read %d pedigrees from %d of %d figures',
        sum(len(one.pedigrees) for one in pedigrees.figures.values()),
        len(accepted),
        len(outcomes),
    )
    return Reading(
        pedigrees=pedigrees,
        construction=_construction(paper.figures, outcomes, accepted[-1][1]),
        usage=[turn for turns in usage for turn in turns],
    )


async def read_paper(
    paper: PaperInput,
    *,
    tokens: ocr.TokenCounter,
    credentials: anthropic_ocr.CredentialsFactory | None = None,
) -> Reading:
    """The worker's reading: every pedigree figure, on a client of its own, within `_TIMEOUT_SECONDS`.

    Args:
        paper: What to read.
        tokens: Where each turn's usage is counted.
        credentials: Builds the access-token provider for the call; None leaves the SDK to resolve credentials from
            the environment.

    Returns:
        The paper's pedigree sets, one per figure, and how they were made.

    Raises:
        ExtractionError: No figure's answer was accepted.
        ocr.SpendRefusedError: The API refused the first turn at a spend limit, or for billing; nothing was spent.
        ocr.SpentError: The reading failed after a turn was paid for, or ran past `_TIMEOUT_SECONDS` after one. The
            original error is its cause.
        anthropic.APIError: The first turn failed before the model answered; nothing was spent.
        TimeoutError: The reading ran past `_TIMEOUT_SECONDS` before the model answered; nothing was spent.
    """
    billed = claude_turns.Billed(tokens)
    try:
        async with (
            anthropic.AsyncAnthropic(
                credentials=credentials() if credentials is not None else None, timeout=_STALL_SECONDS
            ) as client,
            asyncio.timeout(_TIMEOUT_SECONDS),
        ):
            return await read(paper, client=client, tokens=billed)
    except (ExtractionError, ocr.SpentError):
        raise
    except Exception as e:  # noqa: BLE001 — classified and re-raised, never swallowed
        claude_turns.raise_classified(e, billed)
