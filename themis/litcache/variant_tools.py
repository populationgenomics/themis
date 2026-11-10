"""The variant extraction agent's tools: weaver over the reference stores, a page viewer, and the record it writes.

Every tool only reads, except the two that record what the agent found, and those write only into the
`MentionSet` this toolbox builds (docs/design/variant-extraction.md, "What the agent can reach").
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import anthropic.types
import jsonschema
import jsonschema.validators
import protovalidate
import weaver

from themis.litcache import pdf, variant_fit, variant_reference, variant_scan
from themis.litcache.models import variants_pb2

# How many expressions one check call takes, and how many matches find_sequence returns.
_MAX_CHECKS = 50
_MAX_MATCHES = 20
# The shortest stretch find_sequence searches for: shorter ones match a gene by chance.
_MIN_STRETCH = 12
_MAX_SPAN = 600
# The longer side of a zoomed region; inside the high-resolution tier's long edge.
_ZOOM_EDGE = 2400

_OUTCOME_NAMES = {
    variants_pb2.OUTCOME_VALIDATED: 'validated',
    variants_pb2.OUTCOME_NOTHING_TO_CHECK: 'nothing to check',
    variants_pb2.OUTCOME_REFERENCE_DISAGREES: 'reference disagrees',
    variants_pb2.OUTCOME_NOT_CHECKED: 'not checked',
    variants_pb2.OUTCOME_UNPARSEABLE: 'unparseable',
    variants_pb2.OUTCOME_REFERENCE_NOT_HELD: 'reference not held',
}
_DISPOSITIONS = {
    'has_candidates': variants_pb2.DISPOSITION_HAS_CANDIDATES,
    'not_writable': variants_pb2.DISPOSITION_NOT_WRITABLE,
    'registry': variants_pb2.DISPOSITION_REGISTRY,
    'not_sequence_change': variants_pb2.DISPOSITION_NOT_SEQUENCE_CHANGE,
}
_CONTEXTS = {
    name.removeprefix('CONTEXT_').lower(): value
    for name, value in variants_pb2.Context.items()
    if value != variants_pb2.CONTEXT_UNSPECIFIED
}
_REWRITE_KINDS = {
    name.removeprefix('REWRITE_KIND_').lower(): value
    for name, value in variants_pb2.RewriteKind.items()
    if value != variants_pb2.REWRITE_KIND_UNSPECIFIED and value not in variant_fit.DERIVED_REWRITES
}
# The names the agent may give a listed species by: the enum's own, and the scientific one, lower-cased.
SPECIES = {
    **{
        name.removeprefix('SPECIES_').lower(): value
        for name, value in variants_pb2.Species.items()
        if value != variants_pb2.SPECIES_UNSPECIFIED
    },
    'homo sapiens': variants_pb2.SPECIES_HUMAN,
    'mus musculus': variants_pb2.SPECIES_MOUSE,
    'rattus norvegicus': variants_pb2.SPECIES_RAT,
    'danio rerio': variants_pb2.SPECIES_ZEBRAFISH,
    'drosophila melanogaster': variants_pb2.SPECIES_FRUIT_FLY,
    'caenorhabditis elegans': variants_pb2.SPECIES_NEMATODE,
    'saccharomyces cerevisiae': variants_pb2.SPECIES_BUDDING_YEAST,
}
_COMPLEMENT = str.maketrans('ACGT', 'TGCA')
# What an unparseable expression's shape says the agent should do instead.
_IN_TRANS = re.compile(r'\]\s*;\s*\[')
_WITH_CONSEQUENCE = re.compile(r'[cgmnr]\.\S*?\(p\.')
_ADVICE = (
    (_IN_TRANS, 'alleles in trans are not one variant: record each allele as its own mention'),
    (
        _WITH_CONSEQUENCE,
        'one expression per candidate: a nucleotide change and its protein consequence are separate mentions',
    ),
)

ToolResult = str | list[anthropic.types.TextBlockParam | anthropic.types.ImageBlockParam]


def _is_integer(_checker: object, instance: object) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


# Draft 2020-12 counts 2.0 as an integer; the handlers slice and index with these values, so only an int is one.
_Validator = jsonschema.validators.extend(
    jsonschema.Draft202012Validator,
    type_checker=jsonschema.Draft202012Validator.TYPE_CHECKER.redefine('integer', _is_integer),
)


class ToolInputError(ValueError):
    """The agent called a tool with input it can correct; the message goes back to it as the tool's error."""


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {'anyOf': [schema, {'type': 'null'}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


_REWRITE = _object(
    {
        'kind': {'type': 'string', 'enum': sorted(_REWRITE_KINDS)},
        'declared': {
            'type': 'boolean',
            'description': 'true if the paper declares this convention, build or reference',
        },
        'note': {'type': 'string', 'description': 'what was done, in one sentence'},
    }
)
_CANDIDATE = _object(
    {
        'hgvs': {'type': 'string', 'minLength': 1, 'description': 'the full expression, accession included'},
        'rewrites': {'type': 'array', 'items': _REWRITE},
        'flanking': _nullable(
            {'type': 'string', 'description': 'the printed stretch of bases the position was located by'}
        ),
    }
)
_BOX = _object({side: {'type': 'number', 'minimum': 0, 'maximum': 1} for side in ('left', 'top', 'right', 'bottom')})


def _mention_schema(*, lines: bool, pages: bool) -> dict[str, Any]:
    """One record_mentions entry, located by its markdown line, or failing that by its page, as the agent reads them.

    Every field is required except species, which is omitted for a human variant.
    """
    where: dict[str, Any] = {}
    if lines:
        line: dict[str, Any] = {
            'type': 'integer',
            'minimum': 1,
            'description': (
                'the markdown line it is printed on; for a string broken across two, the first'
                + ('; null only for a string the markdown does not print' if pages else '')
            ),
        }
        where['line'] = _nullable(line) if pages else line
    if pages:
        page: dict[str, Any] = {
            'type': 'integer',
            'minimum': 1,
            'description': 'the pdf page it is printed on' + ('; only when line is null, else null' if lines else ''),
        }
        where['page'] = _nullable(page) if lines else page
        where['box'] = _nullable(
            {
                **_BOX,
                'description': (
                    "for a string read from a figure's image: where it is on its page, as fractions of the page from "
                    'its top-left corner; else null'
                ),
            }
        )
    schema = _object(
        {
            'verbatim': {'type': 'string', 'minLength': 1, 'description': 'the mention exactly as printed'},
            'gene': _nullable({'type': 'string'}),
            **where,
            'species': {
                'type': 'string',
                'description': (
                    'for a variant in another organism: '
                    + ', '.join(n for n in SPECIES if ' ' not in n and n != 'human')
                    + ' where it is one, else the organism as printed; omit for a human variant'
                ),
            },
            'context': {
                'type': 'string',
                'enum': sorted(_CONTEXTS),
                'description': (
                    "what this printing says of the variant: observed (in the paper's own individuals, families, "
                    'cohorts or samples); cited (reported by other work); engineered (made for an experiment); unclear'
                ),
            },
            'disposition': {'type': 'string', 'enum': sorted(_DISPOSITIONS)},
            'reason': {'type': 'string', 'description': 'why, for a disposition other than has_candidates; else ""'},
            'candidates': {'type': 'array', 'items': _CANDIDATE},
            'alternative': {
                'type': 'integer',
                'minimum': 1,
                'description': (
                    'for a string naming alternative changes at one position (R482Q/W): which one this entry is, '
                    'counting from 1 in printed order; omit otherwise'
                ),
            },
        }
    )
    schema['required'].remove('species')
    schema['required'].remove('alternative')
    return schema


def _record_mentions_tool(*, lines: bool, pages: bool) -> anthropic.types.ToolParam:
    unit = 'line' if lines else 'page'
    return {
        'name': 'record_mentions',
        'description': (
            'Record variant mentions, one entry per place a mention is printed, with every candidate written for '
            'it, failed ones included, in the order written. Entries with the same string and gene are merged '
            'into one mention with several occurrences, so record each occurrence; its candidates need writing '
            f'only once. A string that prints no position is merged only with entries from the same {unit}. Call as '
            'often as needed; each call adds to what is recorded.'
        ),
        'input_schema': _object({'mentions': {'type': 'array', 'items': _mention_schema(lines=lines, pages=pages)}}),
    }


_TOOLS: list[anthropic.types.ToolParam] = [
    {
        'name': 'gene_transcripts',
        'description': (
            "List a gene's transcripts in the current RefSeq and Ensembl releases on GRCh38, MANE Select first "
            'and RefSeq before Ensembl, each with its protein and the transcript position of the A of its start '
            'codon (c.1).'
        ),
        'input_schema': _object({'gene': {'type': 'string', 'description': 'an HGNC symbol'}}),
    },
    {
        'name': 'held_versions',
        'description': 'List every version of an accession that the reference stores hold.',
        'input_schema': _object({'accession': {'type': 'string'}}),
    },
    {
        'name': 'check',
        'description': (
            'Check HGVS expressions against the reference stores with weaver. Each result is one of: '
            "validated (the stated reference base or residue is the sequence's); nothing to check (parsed and in "
            'range, but no reference base is stated); reference disagrees (the sequence holds something else '
            'there: evidence the reading is wrong); not checked (weaver could not run the check: says nothing '
            'about the expression, so keep it); unparseable; reference not held. Up to '
            f'{_MAX_CHECKS} expressions per call.'
        ),
        'input_schema': _object({'expressions': {'type': 'array', 'items': {'type': 'string'}}}),
    },
    {
        'name': 'project',
        'description': (
            'Project an HGVS expression to another coordinate system with weaver: c. to g. (on GRCh38 or GRCh37), '
            'c. to p., or g. to c. on a named transcript.'
        ),
        'input_schema': _object(
            {
                'expression': {'type': 'string'},
                'to': {'type': 'string', 'enum': ['g38', 'g37', 'p', 'c']},
                'transcript': _nullable({'type': 'string', 'description': 'the transcript, for to="c"'}),
            }
        ),
    },
    {
        'name': 'reference_sequence',
        'description': (
            'Read bases or residues of an accession, 1-based and inclusive. For a transcript, numbering="c" counts '
            'from the A of the start codon (c.1); numbering="native" counts from its first base. Use it to locate a '
            'position, never to supply a base a candidate states.'
        ),
        'input_schema': _object(
            {
                'accession': {'type': 'string'},
                'start': {'type': 'integer'},
                'end': {'type': 'integer'},
                'numbering': {'type': 'string', 'enum': ['c', 'native']},
            }
        ),
    },
    {
        'name': 'find_sequence',
        'description': (
            "Find a printed stretch of bases (at least 12) in a gene's current transcripts, on either strand. "
            "Returns the c. position of the stretch's first base on each transcript where it matches."
        ),
        'input_schema': _object({'gene': {'type': 'string'}, 'sequence': {'type': 'string'}}),
    },
    {
        'name': 'record_declarations',
        'description': (
            'Record the genome builds and numbering conventions the paper declares, and any reference sequence it '
            'names that the printed-references table lacks, each with the sentence that declares it, verbatim. Call '
            'once, before recording mentions; call again to add more.'
        ),
        'input_schema': _object(
            {
                'references': {
                    'type': 'array',
                    'items': _object(
                        {
                            'accession': {'type': 'string'},
                            'gene': _nullable({'type': 'string'}),
                            'sentence': {'type': 'string'},
                        }
                    ),
                },
                'builds': {
                    'type': 'array',
                    'items': _object({'build': {'type': 'string'}, 'sentence': {'type': 'string'}}),
                },
                'conventions': {
                    'type': 'array',
                    'items': _object({'gene': _nullable({'type': 'string'}), 'sentence': {'type': 'string'}}),
                },
            }
        ),
    },
]

_DISMISS_HITS: anthropic.types.ToolParam = {
    'name': 'dismiss_hits',
    'description': (
        'Dismiss scan hits that are not variant mentions (a patient id, a figure panel label, a gene or protein name), '
        'each with a short reason. A hit printed by a mention you record is covered by it: never dismiss a variant.'
    ),
    'input_schema': _object(
        {
            'dismissals': {
                'type': 'array',
                'items': _object({'hit': {'type': 'integer'}, 'reason': {'type': 'string', 'minLength': 1}}),
            }
        }
    ),
}

_VIEW_PAGE: anthropic.types.ToolParam = {
    'name': 'view_page',
    'description': (
        'View part of a page enlarged, to read small print, a table or a sequencing trace. The box is fractions of '
        'the page from its top-left corner.'
    ),
    'input_schema': _object(
        {
            'page': {'type': 'integer'},
            'left': {'type': 'number'},
            'top': {'type': 'number'},
            'right': {'type': 'number'},
            'bottom': {'type': 'number'},
        }
    ),
}


def surface() -> list[object]:
    """Every tool definition the agent can be offered, for every reading, and the advice a check result can carry.

    What the extraction version digests: a change here changes what the agent is told.
    """
    combos = [(lines, pages) for lines in (True, False) for pages in (True, False)]
    return [
        *_TOOLS,
        *(_record_mentions_tool(lines=lines, pages=pages) for lines, pages in combos),
        _DISMISS_HITS,
        _VIEW_PAGE,
        [advice for _shape, advice in _ADVICE],
        [shape.pattern for shape, _advice in _ADVICE],
    ]


def _organism(species: str | None) -> dict[str, Any]:
    """A mention's organism field from an entry's species: human when omitted, a listed name, or the name as printed."""
    if species is None:
        return {'known_species': variants_pb2.SPECIES_HUMAN}
    known = SPECIES.get(' '.join(species.split()).lower())
    return {'known_species': known} if known is not None else {'printed_species': species}


def _row(entry: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Where an entry is printed, for keying a string that prints no position: its line and its page."""
    return entry.get('line'), entry.get('page')


def _merge(mention: variants_pb2.Mention, entry: Mapping[str, Any]) -> None:
    """Add one recorded entry to its mention: an occurrence, and any candidate the mention does not yet hold."""
    box = entry.get('box')
    mention.occurrences.add(
        line=entry.get('line'),
        page=entry.get('page'),
        box=variants_pb2.Box(**box) if box is not None else None,
        context=_CONTEXTS[entry['context']],
    )
    held = {c.hgvs for c in mention.candidates}
    for c in entry['candidates']:
        if c['hgvs'] in held:
            continue
        held.add(c['hgvs'])
        candidate = mention.candidates.add(hgvs=c['hgvs'], flanking=c['flanking'])
        for r in c['rewrites']:
            candidate.rewrites.add(kind=_REWRITE_KINDS[r['kind']], declared=r['declared'], note=r['note'])
    # A mention any entry wrote a candidate for has candidates, whatever an entry said; one without takes the latest
    # entry's disposition, so a mention first called has_candidates with none can be recorded again as what it is.
    if mention.candidates:
        mention.disposition = variants_pb2.DISPOSITION_HAS_CANDIDATES
    else:
        mention.disposition = _DISPOSITIONS[entry['disposition']]
        mention.reason = entry['reason']


def _violation(violation: protovalidate.Violation) -> str:
    proto = violation.proto
    if proto is None:
        return 'the mention set breaks a rule'
    elements = proto.field.elements if proto.field is not None else []
    field = elements[-1].field_name if elements else 'input'
    return f'{field}: {proto.message}'


def _revcomp(sequence: str) -> str:
    return sequence.translate(_COMPLEMENT)[::-1]


class Toolbox:
    """One paper's tools, and the mention set the agent records through them."""

    def __init__(
        self,
        references: variant_reference.References,
        *,
        markdown: str | None,
        pdf_bytes: bytes | None,
        hits: Sequence[variant_scan.Hit] | None,
    ) -> None:
        """Bind the tools to the stores, and to the paper's markdown and pdf as far as the agent reads them.

        Args:
            references: The reference stores.
            markdown: The markdown the agent reads, whose lines it locates mentions by; None when it reads only pages.
            pdf_bytes: The pdf the page viewer renders from, or None when the agent reads only markdown.
            hits: The scan's hits the agent must cover or dismiss, or None when it is given no scan.
        """
        self._references = references
        self._lines = markdown.split('\n') if markdown is not None else None
        self._pdf_bytes = pdf_bytes
        self._hits = {hit.id: hit for hit in hits} if hits is not None else None
        self._dismissed: set[int] = set()
        self.mention_set = variants_pb2.MentionSet()
        # A recorded mention by its printed string (whitespace aside) and gene: a second entry for the same string in
        # the same gene is another occurrence of it, not another mention. A string with no digit names no position,
        # so it names a variant only with its line, sentence or row ('Arg→His' in two rows is two variants), and is
        # keyed by that too.
        # Each recorded mention's index in the mention set, by what makes two entries one mention.
        self._by_key: dict[tuple[str, str | None, tuple[int | None, int | None] | None, int | None], int] = {}
        self._handlers: dict[str, Callable[[Mapping[str, Any]], ToolResult]] = {
            'gene_transcripts': self._gene_transcripts,
            'held_versions': self._held_versions,
            'check': self._check,
            'project': self._project,
            'reference_sequence': self._reference_sequence,
            'find_sequence': self._find_sequence,
            'record_declarations': self._record_declarations,
            'record_mentions': self._record_mentions,
        }
        if pdf_bytes is not None:
            self._handlers['view_page'] = self._view_page
        if hits is not None:
            self._handlers['dismiss_hits'] = self._dismiss_hits
        self._validators = {tool['name']: _Validator(tool['input_schema']) for tool in self.definitions}

    @property
    def definitions(self) -> list[anthropic.types.ToolParam]:
        """The tools offered to the model.

        Their schemas are not strict: the API refuses to compile strict schemas for this many tools, so `call` checks
        every input against its schema instead.
        """
        return [
            *_TOOLS,
            _record_mentions_tool(lines=self._lines is not None, pages=self._pdf_bytes is not None),
            *([_DISMISS_HITS] if self._hits is not None else []),
            *([_VIEW_PAGE] if self._pdf_bytes is not None else []),
        ]

    def unwritten(self) -> list[variants_pb2.Mention]:
        """The mentions recorded as having candidates that have none, in order."""
        return [
            m
            for m in self.mention_set.mentions
            if m.disposition == variants_pb2.DISPOSITION_HAS_CANDIDATES and not m.candidates
        ]

    def outstanding(self) -> list[variant_scan.Hit]:
        """The scan's hits no recorded mention prints and the agent has not dismissed, in order; none without a scan."""
        if self._hits is None:
            return []
        verbatims = [m.verbatim for m in self.mention_set.mentions]
        return [
            hit
            for hit in self._hits.values()
            if hit.id not in self._dismissed and not any(variant_scan.covers(v, hit) for v in verbatims)
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Run one tool call.

        Raises:
            ToolInputError: For input the agent can correct, including an unknown tool.
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolInputError(f'no tool is named {name}')
        problems = sorted(self._validators[name].iter_errors(arguments), key=lambda e: list(e.path))
        if problems:
            raise ToolInputError(
                '; '.join(f'{"/".join(map(str, e.path)) or "input"}: {e.message}' for e in problems[:5])
            )
        return handler(arguments)

    def _gene_transcripts(self, arguments: Mapping[str, Any]) -> str:
        transcripts = self._references.transcripts(arguments['gene'])
        if not transcripts:
            raise ToolInputError(f'no transcripts for the symbol {arguments["gene"]!r}; try the current HGNC symbol')
        rows = []
        for t in transcripts:
            rows.append(
                {
                    'transcript': t.accession,
                    'protein': t.protein,
                    'mane_select': t.mane_select,
                    'c1_at': self._references.cds_start(t.accession),
                    'store': t.store,
                }
            )
        return json.dumps(rows)

    def _held_versions(self, arguments: Mapping[str, Any]) -> str:
        return json.dumps(self._references.held_versions(arguments['accession']))

    def _check(self, arguments: Mapping[str, Any]) -> str:
        expressions: Sequence[str] = arguments['expressions']
        if len(expressions) > _MAX_CHECKS:
            raise ToolInputError(f'at most {_MAX_CHECKS} expressions per call')
        results = []
        for expression in expressions:
            verdict = self._references.check(expression)
            detail = verdict.detail
            if verdict.outcome == variants_pb2.OUTCOME_UNPARSEABLE:
                detail = '; '.join([*(advice for shape, advice in _ADVICE if shape.search(expression)), detail])
            results.append({'expression': expression, 'result': _OUTCOME_NAMES[verdict.outcome], 'detail': detail})
        return json.dumps(results)

    def _project(self, arguments: Mapping[str, Any]) -> str:
        expression, to = arguments['expression'], arguments['to']
        try:
            variant = weaver.parse(expression)
            mapper = self._references.mapper(variant.ac)
            if to in ('g38', 'g37'):
                build = 'GRCh38' if to == 'g38' else 'GRCh37'
                projected = mapper.c_to_g(variant, None)
                if self._references.build_of(projected.ac) != build:
                    raise ToolInputError(f'{variant.ac} is held on {self._references.build_of(projected.ac)} only')
                return projected.format()
            if to == 'p':
                return mapper.c_to_p(variant, None).format()
            transcript = arguments.get('transcript')
            if not transcript:
                raise ToolInputError('to="c" needs a transcript')
            return self._references.mapper(transcript).g_to_c(variant, transcript).format()
        except KeyError as error:
            raise ToolInputError(f'no store holds {error.args[0]}') from error
        except (weaver.HGVSError, ValueError) as error:
            # weaver raises ValueError for a variant of the wrong kind for the projection asked of it.
            raise ToolInputError(f'{type(error).__name__}: {error}') from error

    def _reference_sequence(self, arguments: Mapping[str, Any]) -> str:
        accession, start, end = arguments['accession'], arguments['start'], arguments['end']
        if end - start + 1 > _MAX_SPAN:
            raise ToolInputError(f'at most {_MAX_SPAN} positions per call')
        try:
            if arguments['numbering'] == 'c':
                c1 = self._references.cds_start(accession)
                if c1 is None:
                    raise ToolInputError(f'{accession} is not coding; use numbering="native"')
                start, end = start + c1 - 1, end + c1 - 1
            return self._references.sequence(accession, start, end)
        except KeyError as error:
            raise ToolInputError(f'no store holds {accession}') from error
        except (ValueError, weaver.HGVSError) as error:
            raise ToolInputError(str(error)) from error

    def _find_sequence(self, arguments: Mapping[str, Any]) -> str:
        stretch = ''.join(arguments['sequence'].upper().split())
        if len(stretch) < _MIN_STRETCH or set(stretch) - set('ACGT'):
            raise ToolInputError(f'give at least {_MIN_STRETCH} bases, A/C/G/T only')
        matches = []
        for t in self._references.transcripts(arguments['gene']):
            c1 = self._references.cds_start(t.accession)
            sequence = self._references.sequence(t.accession, 1)
            for strand, probe in (('+', stretch), ('-', _revcomp(stretch))):
                at = sequence.find(probe)
                while at != -1 and len(matches) < _MAX_MATCHES:
                    native = at + 1
                    position = native - c1 + 1 if c1 is not None and native >= c1 else None
                    matches.append({'transcript': t.accession, 'strand': strand, 'native': native, 'c': position})
                    at = sequence.find(probe, at + 1)
        return json.dumps(matches)

    def _view_page(self, arguments: Mapping[str, Any]) -> ToolResult:
        if self._pdf_bytes is None:
            raise ToolInputError('this paper is read without its pages')
        box = (arguments['left'], arguments['top'], arguments['right'], arguments['bottom'])
        try:
            image = pdf.render_region(self._pdf_bytes, page=arguments['page'], box=box, max_edge=_ZOOM_EDGE).result()
        except ValueError as error:
            raise ToolInputError(str(error)) from error
        return [
            {'type': 'text', 'text': f'Page {image.number}, region {box}:'},
            {
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/png',
                    'data': base64.standard_b64encode(image.png).decode('ascii'),
                },
            },
        ]

    def _atomically(self, record: Callable[[], str]) -> str:
        """Apply one record call whole, or not at all: what it leaves must be a mention set the proto accepts."""
        saved, keys = self.mention_set.SerializeToString(), dict(self._by_key)
        try:
            answer = record()
            protovalidate.validate(self.mention_set)
        except BaseException as error:
            self.mention_set.Clear()
            self.mention_set.MergeFromString(saved)
            self._by_key = keys
            if isinstance(error, protovalidate.ValidationError):
                broken = '; '.join(_violation(v) for v in error.violations[:5])
                raise ToolInputError(f'nothing in this call was recorded: {broken}') from error
            if isinstance(error, (ValueError, TypeError)) and not isinstance(error, ToolInputError):
                # The proto's own refusal of a value, such as an integer past int32's range: the agent can correct it.
                raise ToolInputError(f'nothing in this call was recorded: {error}') from error
            raise
        return answer

    def _record_declarations(self, arguments: Mapping[str, Any]) -> str:
        return self._atomically(lambda: self._declare(arguments))

    def _declare(self, arguments: Mapping[str, Any]) -> str:
        for r in arguments['references']:
            self.mention_set.references.add(accession=r['accession'], gene=r['gene'], sentence=r['sentence'])
        for b in arguments['builds']:
            self.mention_set.builds.add(build=b['build'], sentence=b['sentence'])
        for c in arguments['conventions']:
            self.mention_set.conventions.add(gene=c['gene'], sentence=c['sentence'])
        return 'recorded'

    def _misplaced(self, entry: Mapping[str, Any]) -> bool:
        """Whether an entry located by a line names one that does not print its verbatim."""
        if self._lines is None or entry.get('line') is None:
            return False
        line = entry['line']
        if line > len(self._lines):
            return True
        window = ' '.join(self._lines[line - 1 : line + 1])
        pieces = [variant_scan.normalise(piece) for piece in entry['verbatim'].split()]
        if not any(pieces):
            return ''.join(entry['verbatim'].split()) not in ''.join(window.split())
        printed = variant_scan.normalise(window)
        return not all(piece in printed for piece in pieces)

    def _record_mentions(self, arguments: Mapping[str, Any]) -> str:
        return self._atomically(lambda: self._record(arguments))

    def _record(self, arguments: Mapping[str, Any]) -> str:
        for m in arguments['mentions']:
            if (m.get('line') is None) == (m.get('page') is None):
                raise ToolInputError(
                    f'{m["verbatim"]!r}: give its line where the markdown prints it, else its page; exactly one'
                )
            if m.get('box') is not None and m.get('page') is None:
                raise ToolInputError(f'{m["verbatim"]!r} has a box on no page: give the page it is on')
            if m['disposition'] == 'not_writable' and variant_scan.parses_as_hgvs(m['verbatim']):
                raise ToolInputError(
                    f'{m["verbatim"]!r} parses as HGVS, so it is writable: write a candidate for it, whatever its '
                    'check says'
                )
        misplaced = [m for m in arguments['mentions'] if self._misplaced(m)]
        for m in arguments['mentions']:
            if self._misplaced(m):
                continue
            printed = ''.join(m['verbatim'].split())
            key = (
                printed,
                m['gene'],
                _row(m) if not any(c.isdigit() for c in printed) else None,
                m.get('alternative'),
            )
            index = self._by_key.get(key)
            if index is None:
                self.mention_set.mentions.add(verbatim=m['verbatim'], gene=m['gene'], **_organism(m.get('species')))
                index = self._by_key[key] = len(self.mention_set.mentions) - 1
            _merge(self.mention_set.mentions[index], m)
        recorded = f'recorded; {len(self.mention_set.mentions)} mentions so far{self._hits_left()}'
        if not misplaced:
            return recorded
        # The rest of the batch stands, so only these need sending again.
        named = '; '.join(f'{m["verbatim"]!r} on line {m["line"]}' for m in misplaced)
        return f'{recorded}. Not recorded, because the line does not print the verbatim: {named}. Record them again.'

    def _hits_left(self) -> str:
        return f'; {len(self.outstanding())} scan hits outstanding' if self._hits is not None else ''

    def _dismiss_hits(self, arguments: Mapping[str, Any]) -> str:
        if self._hits is None:
            raise ToolInputError('this paper has no scan hits')
        for d in arguments['dismissals']:
            if d['hit'] not in self._hits:
                raise ToolInputError(f'no scan hit is numbered {d["hit"]}')
        for d in arguments['dismissals']:
            if d['hit'] in self._dismissed:
                continue
            hit = self._hits[d['hit']]
            self._dismissed.add(hit.id)
            self.mention_set.dismissals.add(text=hit.text, line=hit.line, reason=d['reason'])
        return f'dismissed{self._hits_left()}'
