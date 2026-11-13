"""Reading a paper's pedigrees: each pedigree figure shown to a model with a zoom tool, and the answers checked.

The paper's figure set says which figures hold a pedigree, on which page, and where their legends are
(docs/design/pedigree-extraction.md). One conversation per figure and page it draws pedigrees on, after grus's page
extraction: the model sees that whole page sized to its image tier, and the legend's page after it when that is
another, asks for any region of the drawn page at full resolution with `zoom`, and answers with a grus pedigree set as
JSON. The answer is accepted only when grus's loader accepts it, every cited region lies on the page, and every quote
is in the paper; otherwise the reason goes back to the model to fix. The sets are merged into one for the paper; a
figure whose answer is never accepted is recorded with the reason, and the other figures are kept.
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
from themis.litcache.models import pedigrees_pb2

_LOG = logging.getLogger(__name__)

# A change to what the model does that `_harness_version` does not digest, such as how a tool words its result, bumps
# this.
_REVISION = '1'
MODEL = 'claude-sonnet-5-5'
# No effort is set: the API's default is what grus's reading ladder measured.
_EFFORT: str | None = None
_TIER = claude_images.HIGH_RESOLUTION
_MAX_TOKENS = 64_000
# The page, the legend's page when it is another, and the zooms: the twenty images grus's ladder read with.
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
        'Re-render a region of the page at higher resolution, from the source document. `region` is '
        '[x0, y0, x1, y1]: the top-left and bottom-right corners in pixel coordinates of the page image (the '
        "first image), always in the page image's pixels, never a zoomed image's. Returns the region as an "
        'image, scaled to the largest size you accept.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'region': {
                'type': 'array',
                'items': {'type': 'number'},
                'minItems': 4,
                'maxItems': 4,
                'description': '[x0, y0, x1, y1] in pixel coordinates of the page image.',
            }
        },
        'required': ['region'],
    },
}


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


def _instruction(figure: str, page: page_render.Image, boxes: str, legend: page_render.Image | None, zooms: int) -> str:
    legend_note = (
        f' The second image is the page that carries its legend, {legend.width} x {legend.height} px, for reading '
        'only: zoom and cite regions on the first image.'
        if legend is not None
        else ''
    )
    return (
        f'The first image is the page, {page.width} x {page.height} px. Transcribe the pedigrees of {figure} into '
        f'the required JSON: its pedigree panels are at about {boxes} in pixel coordinates of the page image, and '
        f'any other figure on the page is read separately.{legend_note} Report meaning only, never geometry. You may '
        f'call `zoom` up to {zooms} times; give each region as [x0, y0, x1, y1] in pixel coordinates of the page '
        'image.'
    )


def _repair(error: Exception) -> str:
    return (
        f'Your JSON was rejected:\n{error}\n\nRe-read the page (zoom again if you need to) and reply with the '
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
        'instruction': [
            _instruction('Figure 0', probe, '[]', probe, 0),
            _instruction('Figure 0', probe, '[]', None, 0),
        ],
        'tool': ZOOM_TOOL,
        'support_fields': sorted(ir.SUPPORT_FIELDS),
        'grus': importlib.metadata.version('grus'),
    }
    digest = hashlib.sha256(json.dumps(given, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f'{_REVISION}.{digest[:12]}'


HARNESS_VERSION = _harness_version()


class ExtractionError(Exception):
    """A reading ended without a pedigree set it can record: every figure failed, or their sets cannot be merged.

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
class FigurePage:
    """One page a pedigree figure is drawn on, as one conversation reads it.

    Attributes:
        figure: The figure's index in the paper's figure set.
        label: The figure's label as printed, or empty.
        page: The page its pedigree panels on this page are drawn on.
        boxes: Those panels' boxes, as fractions of the page from its top-left corner.
        legend: The page carrying the figure's legend, when that is another page.
    """

    figure: int
    label: str
    page: pdf.PageLayout
    boxes: Sequence[tuple[float, float, float, float]]
    legend: pdf.PageLayout | None = None


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
        figures: The pages of its pedigree figures to read, in figure order.
        variants: Its validated variants, which the reader names alleles by; empty for a paper with none.
    """

    markdown: str
    pdf_bytes: bytes
    pdf_hash: str
    figures: Sequence[FigurePage]
    variants: Sequence[KnownVariant]


@dataclasses.dataclass(frozen=True)
class Reading:
    """A paper's pedigrees, read.

    Attributes:
        pedigree_set: Every pedigree read from the figures whose answers were accepted, with the conditions they
            share and the citations that place them.
        construction: How it was made, for the record: every figure read, a failed one with its reason.
        usage: Each turn's token usage: each conversation's turns in order, the conversations in the order of the
            paper's figure pages.
    """

    pedigree_set: pedigree_pb2.PedigreeSet
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
    """One figure page's conversation: what it shows, the zooms it made, and the image bytes every turn re-sends."""

    def __init__(
        self,
        pdf_bytes: bytes,
        pdf_hash: str,
        unit: FigurePage,
        overview: page_render.Image,
        legend: page_render.Image | None,
        variants_text: str,
    ) -> None:
        self._pdf = pdf_bytes
        self._variants_text = variants_text
        self.pdf_hash = pdf_hash
        self.unit = unit
        self.overview = overview
        self.legend = legend
        self.zoom_budget = _MAX_IMAGES - (1 if legend is None else 2)
        self.zooms = 0
        self.wire_bytes = overview.wire_bytes + (0 if legend is None else legend.wire_bytes)

    @property
    def name(self) -> str:
        figure = self.unit.label or f'figure {self.unit.figure}'
        return f'{figure} on page {self.unit.page.number}'

    def first(self) -> list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam]:
        """The opening turn: the paper's variants, the page, the legend's page when it is another, and the instruction.

        The variants come first, with a cache point of their own, so each figure of a paper reads them from the cache.
        """
        w, h = self.overview.width, self.overview.height
        boxes = ', '.join(
            f'[{round(left * w)}, {round(top * h)}, {round(right * w)}, {round(bottom * h)}]'
            for left, top, right, bottom in self.unit.boxes
        )
        figure = f'"{self.unit.label}"' if self.unit.label else 'the unlabelled figure'
        content: list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam] = [
            {'type': 'text', 'text': self._variants_text, 'cache_control': {'type': 'ephemeral'}},
            self.overview.block(),
        ]
        if self.legend is not None:
            content.append(self.legend.block())
        text = _instruction(figure, self.overview, boxes, self.legend, self.zoom_budget)
        content.append({'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral'}})
        return content

    async def zoom(self, block: anthropic.types.ToolUseBlock) -> anthropic.types.ToolResultBlockParam:
        """Answer one zoom call: the region as an image, or the reason it was refused."""

        def error(message: str) -> anthropic.types.ToolResultBlockParam:
            return {'type': 'tool_result', 'tool_use_id': block.id, 'content': message, 'is_error': True}

        if block.name != ZOOM_TOOL['name']:
            return error(f'unknown tool {block.name!r}; the only tool is `zoom`')
        if self.zooms >= self.zoom_budget:
            return error('the zoom budget is spent; reply with the JSON now')
        region = block.input.get('region') if isinstance(block.input, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        try:
            box = _overview_box(region, self.overview)
            image = await page_render.render(self._pdf, self.unit.page, box, tier=_TIER)
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
        note = f'Region {region} of the page image.'
        if image.native_limited:
            note += ' Native resolution reached: zooming further into this area adds no detail.'
        return {
            'type': 'tool_result',
            'tool_use_id': block.id,
            'content': [image.block(), {'type': 'text', 'text': note}],
        }

    def accept(self, text: str, paper_text: str) -> pedigree_pb2.PedigreeSet:
        """The answer as a pedigree set, its regions placed on the page and its quotes found in the paper.

        Raises:
            _RejectedError: grus's loader refuses it, a region is off the page image, or a quote is not in the paper.
        """
        try:
            answer = ir.load_set_json(_json_object(text))
        except (json_format.ParseError, ir.ValidationError, ir.IntegrityError) as e:
            raise _RejectedError(str(e)) from e
        shown = [self.unit.page] + ([] if self.unit.legend is None else [self.unit.legend])
        haystack = variant_scan.normalise(paper_text) + ''.join(variant_scan.normalise(p.text) for p in shown)
        layout = self.unit.page
        for citation in answer.citations:
            citation.document_id = self.pdf_hash
            if citation.HasField('region'):
                r = citation.region
                try:
                    left, top, right, bottom = _overview_box([r.x0, r.y0, r.x1, r.y1], self.overview)
                except _RejectedError as e:
                    raise _RejectedError(f'citation {citation.id!r}: {e}') from e
                r.page = layout.number - 1  # grus counts pages from 0
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


async def _open(pdf_bytes: bytes, pdf_hash: str, unit: FigurePage, variants_text: str) -> _Conversation:
    """Render what a conversation shows.

    Raises:
        _FailedError: A page cannot be rendered, or renders too large to send.
    """
    images: list[page_render.Image] = []
    for layout in [unit.page] + ([] if unit.legend is None else [unit.legend]):
        try:
            image = await page_render.render(pdf_bytes, layout, (0.0, 0.0, 1.0, 1.0), tier=_TIER)
        except (ValueError, pypdfium2.PdfiumError) as e:
            raise _FailedError(f'page {layout.number} could not be rendered: {e}') from e
        if image.wire_bytes > claude_images.MAX_IMAGE_BASE64_BYTES:
            raise _FailedError(f'page {layout.number} renders too large to send')
        images.append(image)
    return _Conversation(pdf_bytes, pdf_hash, unit, images[0], images[1] if len(images) > 1 else None, variants_text)


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


def _declarations(sets: Sequence[pedigree_pb2.PedigreeSet]) -> dict[str, pedigree_pb2.ConditionDef]:
    """One declaration per condition name across the readings, a stated mode of inheritance over an unstated one.

    Two readings stating different modes for one name contradict each other, so the merged declaration states none.
    """
    merged: dict[str, pedigree_pb2.ConditionDef] = {}
    conflicted: set[str] = set()
    for one in sets:
        for condition in one.conditions:
            held = merged.get(condition.name)
            if held is None:
                declaration = pedigree_pb2.ConditionDef(id=f'k{len(merged) + 1}', name=condition.name)
                if condition.HasField('inheritance'):
                    declaration.inheritance = condition.inheritance
                merged[condition.name] = declaration
            elif not condition.HasField('inheritance'):
                continue
            elif not held.HasField('inheritance'):
                held.inheritance = condition.inheritance
            elif held.inheritance != condition.inheritance:
                conflicted.add(condition.name)
    for name in sorted(conflicted):
        _LOG.warning('figures state different modes of inheritance for %r; the merged set states none', name)
        merged[name].ClearField('inheritance')
    return merged


@dataclasses.dataclass(frozen=True)
class _LocusMap:
    """Where one reading's locus went in the merged set: its id there, and its allele symbols as the merged ones."""

    id: str
    symbols: dict[str, str]


def _compatible(held: pedigree_pb2.Allele, allele: pedigree_pb2.Allele) -> bool:
    """Whether two readings' alleles can be one: neither states a name the other contradicts."""
    return not (held.name and allele.name and held.name != allele.name)


def _counterpart(held: pedigree_pb2.Locus, allele: pedigree_pb2.Allele) -> pedigree_pb2.Allele | None:
    """The held allele another reading's allele is, if any.

    The one with its symbol; else, for a reference allele, the held reference; else the one with its printed name.
    """
    for other in held.alleles:
        if other.symbol == allele.symbol:
            return other
    if allele.reference:
        return next((other for other in held.alleles if other.reference), None)
    if allele.name:
        return next((other for other in held.alleles if other.name == allele.name), None)
    return None


def _held_locus(
    merged: Sequence[pedigree_pb2.Locus], locus: pedigree_pb2.Locus, claimed: set[str]
) -> pedigree_pb2.Locus | None:
    """The merged locus another reading's locus is, if any.

    The one with its grus identity; else, for a named locus, the one held locus of that name whose gene the two do not
    contradict. A held locus another locus of the same reading went to (`claimed`) is never it: one reading's two loci
    stay two.
    """
    open_loci = [held for held in merged if held.id not in claimed]
    identity = ir.locus_identity(locus)
    same = next((held for held in open_loci if ir.locus_identity(held) == identity), None)
    if same is not None or not locus.name:
        return same
    gene = locus.gene if locus.HasField('gene') else None
    named = [
        held
        for held in open_loci
        if held.name == locus.name and (gene is None or not held.HasField('gene') or held.gene == gene)
    ]
    return named[0] if len(named) == 1 else None


def _join(held: pedigree_pb2.Locus, locus: pedigree_pb2.Locus) -> dict[str, str] | None:
    """Join a reading's locus into the held one; its symbols as the held ones, or None when the two contradict.

    An allele is the held allele with its symbol, or the held reference when both are references (one figure's "+"
    is another's "WT"), or the held allele of its printed name. What one reading leaves unstated (a name, the
    reference flag, an identifier) is taken from the other; a name the other contradicts is a contradiction, as are
    two of the reading's alleles that would be one held allele, or two held alleles left sharing a name.
    """
    pairs = [(allele, _counterpart(held, allele)) for allele in locus.alleles]
    if any(other is not None and not _compatible(other, allele) for allele, other in pairs):
        return None
    matched = [other.symbol for _, other in pairs if other is not None]
    if len(matched) != len(set(matched)):
        return None
    symbols: dict[str, str] = {}
    for allele, other in pairs:
        if other is None:
            if any(a.symbol == allele.symbol for a in held.alleles) or (
                allele.reference and any(a.reference for a in held.alleles)
            ):
                return None
            held.alleles.add().CopyFrom(allele)
            symbols[allele.symbol] = allele.symbol
            continue
        other.name = other.name or allele.name
        other.reference = other.reference or allele.reference
        known = {(i.scheme, i.value) for i in other.identifiers}
        other.identifiers.extend(i for i in allele.identifiers if (i.scheme, i.value) not in known)
        symbols[allele.symbol] = other.symbol
    if sum(a.reference for a in held.alleles) > 1:
        return None
    names = [a.name for a in held.alleles if a.name]
    if len(names) != len(set(names)):
        return None
    if locus.HasField('gene') and not held.HasField('gene'):
        held.gene = locus.gene
    return symbols


def _loci(sets: Sequence[pedigree_pb2.PedigreeSet]) -> tuple[list[pedigree_pb2.Locus], list[dict[str, _LocusMap]]]:
    """One declaration per locus across the readings, and for each reading, where each of its loci went.

    Loci are one when grus's `locus_identity` says so, or when they share a printed name and one leaves the gene
    unstated. Their alleles are joined (`_join`). A reading whose locus contradicts the held one is left out of its
    map, and its genotypes there are dropped; its labels stay.
    """
    merged: list[pedigree_pb2.Locus] = []
    maps: list[dict[str, _LocusMap]] = []
    for n, one in enumerate(sets, 1):
        mapping: dict[str, _LocusMap] = {}
        for locus in one.loci:
            held = _held_locus(merged, locus, {m.id for m in mapping.values()})
            if held is None:
                if ir.locus_identity(locus) in {ir.locus_identity(other) for other in merged}:
                    _LOG.warning(
                        'reading %d states locus %r twice over; its genotypes there are dropped',
                        n,
                        locus.name or locus.id,
                    )
                    continue
                held = pedigree_pb2.Locus()
                held.CopyFrom(locus)
                held.id = f'l{len(merged) + 1}'
                merged.append(held)
                mapping[locus.id] = _LocusMap(held.id, {a.symbol: a.symbol for a in locus.alleles})
                continue
            joined = pedigree_pb2.Locus()
            joined.CopyFrom(held)
            symbols = _join(joined, locus)
            identities = [ir.locus_identity(other) for other in merged if other is not held]
            if symbols is None or ir.locus_identity(joined) in identities:
                _LOG.warning(
                    'reading %d states locus %r against another reading; its genotypes there are dropped',
                    n,
                    locus.name or locus.id,
                )
                continue
            held.CopyFrom(joined)
            mapping[locus.id] = _LocusMap(held.id, symbols)
        maps.append(mapping)
    return merged, maps


def _genotypes(individual: pedigree_pb2.Individual, loci: dict[str, _LocusMap]) -> None:
    """Point a copied person's genotypes and annotations at the merged loci, dropping those left out of the map."""
    kept = [g for g in individual.genotypes if g.locus_id in loci]
    for genotype in kept:
        where = loci[genotype.locus_id]
        genotype.locus_id = where.id
        genotype.alleles[:] = [where.symbols[a] for a in genotype.alleles]
    del individual.genotypes[:]
    individual.genotypes.extend(kept)
    for annotation in individual.annotations:
        annotation.loci[:] = [loci[i].id for i in annotation.loci if i in loci]


def _merge(sets: Sequence[pedigree_pb2.PedigreeSet]) -> pedigree_pb2.PedigreeSet:
    """One set for the paper: citation ids made unique per reading, and each condition and locus declared once.

    Conditions are one by name, loci by grus's locus identity (`_loci`). An unnamed condition is the figure's sole
    one; within one paper the readings' unnamed conditions are taken as one.

    Raises:
        ExtractionError: The merged set breaks grus's rules, which no attempt on the same answers would change.
    """
    declared = _declarations(sets)
    loci, locus_maps = _loci(sets)
    merged = pedigree_pb2.PedigreeSet(conditions=list(declared.values()), loci=loci)
    for n, one in enumerate(sets, 1):
        conditions = {c.id: declared[c.name] for c in one.conditions}
        locus_ids = locus_maps[n - 1]
        citations = {citation.id: f's{n}.{citation.id}' for citation in one.citations}
        for citation in one.citations:
            added = merged.citations.add()
            added.CopyFrom(citation)
            added.id = citations[citation.id]
        merged.evidence.extend(citations[c] for c in one.evidence)
        for pedigree in one.pedigrees:
            added_pedigree = merged.pedigrees.add()
            added_pedigree.CopyFrom(pedigree)
            for individual in added_pedigree.individuals:
                for entry in individual.conditions:
                    entry.condition_id = conditions[entry.condition_id].id
                _genotypes(individual, locus_ids)
            added_pedigree.evidence[:] = [citations[c] for c in pedigree.evidence]
            added_pedigree.ClearField('supports')
            for support in pedigree.supports:
                kept = added_pedigree.supports.add()
                kept.CopyFrom(support)
                if support.HasField('condition_id'):
                    declaration = conditions[support.condition_id]
                    if support.field == 'condition.inheritance' and not declaration.HasField('inheritance'):
                        del added_pedigree.supports[-1]  # the mode it supported was dropped as contradicted
                        continue
                    kept.condition_id = declaration.id
                if support.loci:
                    kept.loci[:] = [locus_ids[i].id for i in support.loci if i in locus_ids]
                    if not kept.loci:
                        del added_pedigree.supports[-1]  # every locus it supported was dropped as contradicted
                        continue
                kept.citations[:] = [citations[c] for c in support.citations]
    try:
        ir.validate_set(merged)
    except (ir.ValidationError, ir.IntegrityError) as e:
        raise ExtractionError(f'the figures read cannot be merged into one set: {e}') from e
    return merged


def _first(failures: Sequence[BaseException]) -> BaseException:
    """The failure a reading reports for its conversations: a paid one before any other."""
    return next((e for e in failures if isinstance(e, ocr.SpentError)), failures[0])


def _construction(
    units: Sequence[FigurePage], outcomes: Sequence[tuple[pedigree_pb2.PedigreeSet, str] | _FailedError], model: str
) -> pedigrees_pb2.PedigreeExtractionConstruction:
    """Every figure read, in order, each with the pages whose answer was not accepted and why."""
    figures: dict[int, pedigrees_pb2.PedigreeFigure] = {}
    for unit, outcome in zip(units, outcomes, strict=True):
        figure = figures.setdefault(unit.figure, pedigrees_pb2.PedigreeFigure(figure=unit.figure))
        if isinstance(outcome, _FailedError):
            figure.failures.add(pdf_page=unit.page.number, reason=str(outcome))
    return pedigrees_pb2.PedigreeExtractionConstruction(
        model=model, harness_version=HARNESS_VERSION, figures_read=list(figures.values())
    )


async def read(
    paper: PaperInput,
    *,
    client: anthropic.AsyncAnthropic,
    tokens: ocr.TokenCounter,
    model: str = MODEL,
) -> Reading:
    """Read every pedigree figure of a paper, a few conversations at once, into one pedigree set.

    Args:
        paper: What to read, and the rendering quotes are checked against.
        client: The Anthropic client the conversations run on.
        tokens: Where each turn's usage is counted, whatever the outcome.
        model: The model to run.

    Returns:
        The paper's pedigree set from the figures whose answers were accepted, and how it was made.

    Raises:
        ExtractionError: No conversation's answer was accepted, or the accepted sets cannot be merged; another attempt
            is expected to end the same way.
        ocr.SpentError: A turn failed after the model began answering it; its usage so far is counted.
        ValueError: The paper has no figure to read.
    """
    if not paper.figures:
        raise ValueError('a paper with no pedigree figure has no pedigrees to read')
    usage: list[list[anthropic.types.Usage]] = [[] for _ in paper.figures]
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(index: int, unit: FigurePage) -> tuple[pedigree_pb2.PedigreeSet, str] | _FailedError:
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
                failed = _FailedError(f'{unit.label or f"figure {unit.figure}"} on page {unit.page.number}: {e}')
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
    merged = _merge([pedigree_set for pedigree_set, _ in accepted])
    _LOG.info('read %d pedigrees from %d of %d figure pages', len(merged.pedigrees), len(accepted), len(outcomes))
    return Reading(
        pedigree_set=merged,
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
        The paper's pedigree set, and how it was made.

    Raises:
        ExtractionError: No figure's answer was accepted, or the figures cannot be merged.
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
