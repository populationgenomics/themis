"""The SVCv4 classification widget's payload, built by running the classification.

The working document draws one classification from a `Svcv4Classification` payload
(docs/design/svcv4-classification-widget.md). `build` makes it: it runs `builders.classify_variant` on the
inputs it is handed, and every point, total, band and class in the payload comes from that result, so none is
typed by the agent. The agent supplies what the library cannot know, as the payload's own messages with only
their annotation fields set: the routing's identifiers and reasoning, and each code's status, evidence and
reasoning. A code the agent does not restate keeps the annotation the committed payload holds for it, while its
computed fields are unchanged, so a curator's tick on it survives the rebuild.

Write the payload through the widgets helper, which carries the curator's ticks and notes across::

    from themis.agent import widgets
    from themis.svcv4 import widget
    from themis.svcv4.models import svcv4_pb2
    from themis.widgets.models import svcv4_classification_pb2 as svcv4_widget

    Record = svcv4_widget.Svcv4Classification
    inputs = widget.Inputs(consequence=..., evidence=..., independent_codes=[...], gate_level=...)
    widgets.update('assets/svcv4.binpb', lambda committed: widget.build(
        ref, inputs,
        routing=Record.Routing(variant=Record.Variant(...), entity=Record.Entity(...), rationale='...'),
        codes={'CLN_DNV': Record.Code(status=svcv4_pb2.ASSESSMENT_STATUS_SCORED, cells=[...], evidence=[...],
                                      rationale='...', confidence=svcv4_pb2.CONFIDENCE_LEANING), ...},
        sensitivity=[widget.Alternative('parentage confirmed', dataclasses.replace(inputs, independent_codes=[...]))],
        committed=committed,
    ))

EVALUATION ONLY, AGAINST A DRAFT STANDARD: see `themis.svcv4`.
"""

from __future__ import annotations

import dataclasses
import decimal
from collections.abc import Mapping, Sequence

import protovalidate

from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2
from themis.svcv4 import builders, classify, observations, provenance, reference, scoring
from themis.svcv4.data import meta
from themis.svcv4.models import svcv4_pb2
from themis.widgets.models import svcv4_classification_pb2

Record = svcv4_classification_pb2.Svcv4Classification

_SCORED = svcv4_pb2.ASSESSMENT_STATUS_SCORED

# The fields of a code the agent writes. Every other field is computed here, or is a curator's.
_CODE_ANNOTATION = (
    'status',
    'status_reason',
    'decision',
    'cells',
    'evidence',
    'rationale',
    'nearest_alternative',
    'confidence',
    'confidence_note',
)
# The fields of a code computed from the tally, which an annotation carries across only while they are unchanged.
_CODE_COMPUTED = ('points', 'raw_points', 'adjustment', 'basis', 'path_family', 'releases')
_CODE_GUARDS = ('reviewed', 'note')
# What an annotation of a cell may set: which row, and how many observations fell in it.
_CELL_ANNOTATION = ('cell_id', 'count')

_CLASSES: Mapping[str, svcv4_pb2.Classification] = {
    'B': svcv4_pb2.CLASSIFICATION_BENIGN,
    'LB': svcv4_pb2.CLASSIFICATION_LIKELY_BENIGN,
    'VUS': svcv4_pb2.CLASSIFICATION_VUS,
    'LP': svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC,
    'P': svcv4_pb2.CLASSIFICATION_PATHOGENIC,
}
# What the gate's two terminal results are, in the class vocabulary, by the reference's gate level.
_TERMINAL: Mapping[gene_disease_pb2.GateLevel, svcv4_pb2.Classification] = {
    gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED: svcv4_pb2.CLASSIFICATION_VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE,
    gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED: svcv4_pb2.CLASSIFICATION_DO_NOT_REPORT,
}


class AnnotationError(ValueError):
    """An annotation the tally contradicts, or one that sets a field the library computes; the message says how."""


@dataclasses.dataclass(frozen=True)
class Inputs:
    """What `builders.classify_variant` takes, as one value an alternative varies.

    Attributes:
        consequence: The routing consequence, as `Variant.Normalize` states it.
        evidence: The workflow's judgement inputs, in the family its arm takes.
        gate_level: The resolved entity's gene-disease-validity gate level.
        independent_codes: The POP/CLN/LOC codes, as the doors return them or as `classify.IndependentCode`s.
        whole_gene: A whole-gene deletion: SM13's own tier table, which `classify_variant` cannot reach and
            `builders.build_exon_deletion(whole_gene=True)` does. Only with `NullEvidence`.
    """

    consequence: evidence_pb2.Consequence
    evidence: builders.VariantEvidence
    gate_level: gene_disease_pb2.GateLevel
    independent_codes: builders.IndependentCodes | None = None
    whole_gene: bool = False

    def classify(self, ref: reference.Reference) -> classify.Classification:
        """The tally these inputs yield.

        Raises:
            ValueError: As `builders.classify_variant` does, and on a whole-gene deletion whose inputs are not a
                null path on an exon-deletion consequence.
        """
        if not self.whole_gene:
            return builders.classify_variant(
                ref,
                consequence=self.consequence,
                evidence=self.evidence,
                independent_codes=self.independent_codes,
                gate_level=self.gate_level,
            )
        if self.consequence != evidence_pb2.CONSEQUENCE_EXON_DELETION or not isinstance(
            self.evidence, builders.NullEvidence
        ):
            raise ValueError('a whole-gene deletion is an exon-deletion consequence scored on a null path')
        return classify.classify(
            ref,
            builders.build_exon_deletion(
                ref,
                null=self.evidence,
                whole_gene=True,
                independent_codes=self.independent_codes,
                gate_level=self.gate_level,
            ),
        )


@dataclasses.dataclass(frozen=True)
class Alternative:
    """Another value of one or more judgement inputs, and the inputs that state it.

    Attributes:
        assumption: The inputs and the values taken, in words, e.g. "parentage confirmed".
        inputs: The inputs the tally is computed from: the main inputs with the varied ones replaced.
    """

    assumption: str
    inputs: Inputs


def build(
    ref: reference.Reference,
    inputs: Inputs,
    *,
    routing: svcv4_classification_pb2.Svcv4Classification.Routing | None = None,
    codes: Mapping[str, svcv4_classification_pb2.Svcv4Classification.Code] | None = None,
    open_values: Sequence[Alternative] = (),
    sensitivity: Sequence[Alternative] = (),
    committed: svcv4_classification_pb2.Svcv4Classification | None = None,
) -> svcv4_classification_pb2.Svcv4Classification:
    """The payload for the classification `inputs` yield, with every judgement at its default.

    Args:
        ref: The loaded reference.
        inputs: The inputs of the tally the document proceeds on.
        routing: The routing's annotation: the variant, the entity (with no gate level, which `inputs` states),
            and the rationale. None keeps the committed payload's.
        codes: Each code's annotation, by code name: its status, and for a scored code its rationale and
            confidence, with the evidence, the decision-tree cell (`decision`) for a code on a path, and for an
            independent code the cells it was priced from, as `cell_id` and `count`. A code left out keeps the
            committed payload's annotation, while the points, basis and adjustment the tally gives it are the
            ones that annotation was written against.
        open_values: The other surviving values of an input reported open. Each is scored here.
        sensitivity: Each judgement input varied across its plausible range. Each is scored here.
        committed: The payload the last commit holds, which `widgets.update` hands the build; None for a first
            write.

    Returns:
        The payload, validated. `widgets.update` carries the curator's ticks and notes across onto it.

    Raises:
        AnnotationError: If a code the paths taken admit has no annotation, an annotation names a code they do not
            admit, sets a field the library computes or a curator's judgement, states a status the tally
            contradicts, or prices cells that do not sum to the code's points; or a carried annotation was written
            against other points, another path or other releases, or a carried routing against another route,
            consequence or gate level.
        ValueError: As `builders.classify_variant` does, or if the payload fails its rules.
    """
    result = inputs.classify(ref)
    routes = _routes(inputs, result)
    admitted = _admitted(ref, routes)
    lines = _trail(result)
    committed_codes = {} if committed is None else {code.code: code for code in committed.codes}
    unknown = sorted(set(codes or {}) - {code for code, _ in admitted})
    if unknown:
        raise AnnotationError(
            f'{", ".join(unknown)}: not a code the paths taken admit, which are '
            f'{", ".join(code for code, _ in admitted)}'
        )
    paths = [_path(inputs, path, path is result.selected_path) for path in routes]
    now = _path_context([Record.Route(family=_family(path), label=path.label) for path in routes], paths)
    before = {} if committed is None else _path_context(committed.routing.routes, committed.paths)
    releases = _independent_releases(inputs)
    rarity_moved = _rarity_moved(ref, result, committed)
    payload = Record(
        framework=_framework(ref),
        routing=_routing(routing, committed, inputs, routes),
        codes=[
            _code(
                ref,
                code,
                family,
                lines,
                releases.get(code, ()),
                (codes or {}).get(code),
                committed_codes.get(code),
                moved=_moved(family, before, now) or rarity_moved.get(code, ''),
            )
            for code, family in admitted
        ],
        paths=paths,
        lines=_lines(result),
        tally=_tally(ref, result, inputs.gate_level),
        open_values=[_alternative(ref, result, alternative, admitted) for alternative in open_values],
        sensitivity=[_alternative(ref, result, alternative, admitted) for alternative in sensitivity],
        bands=_bands(ref),
    )
    agreed = all(value.tally.final_class == payload.tally.final_class for value in payload.open_values)
    payload.classification = payload.tally.final_class if agreed else svcv4_pb2.CLASSIFICATION_NOT_ESTABLISHED
    _check_sum(payload, result)
    protovalidate.validate(payload)
    return payload


def admitted(ref: reference.Reference, inputs: Inputs) -> tuple[str, ...]:
    """The codes `build` needs an annotation for: every code the paths the inputs take admit, in the reference's order.

    Raises:
        ValueError: As `builders.classify_variant` does.
    """
    result = inputs.classify(ref)
    return tuple(code for code, _ in _admitted(ref, _routes(inputs, result)))


def _decimal(value: decimal.Decimal) -> str:
    """A decimal as the payload carries it: fixed-point text, never an exponent."""
    return format(value, 'f')


def _family(path: scoring.PathResult) -> str:
    return path.parent_code.rstrip('_')


def _routes(inputs: Inputs, result: classify.Classification) -> list[scoring.PathResult]:
    """The paths scored, in the order the workflow states them: a missense variant's amino-acid path first."""
    paths = [path for path in (result.selected_path, result.alternate_path) if path is not None]
    if isinstance(inputs.evidence, builders.MissensePaths):
        paths.sort(key=lambda path: _family(path) != 'MIS')
    return paths


def _admitted(ref: reference.Reference, routes: Sequence[scoring.PathResult]) -> list[tuple[str, str]]:
    """Every code the paths taken admit, in the reference's order, each with its path's family, '' off any path."""
    on_path = {code: _family(path) for path in routes for code in ref.concept_to_codes[_family(path)]}
    return [
        (code, on_path.get(code, ''))
        for code, spec in ref.codes.items()
        if spec.family in ref.independent_families or code in on_path
    ]


def _trail(result: classify.Classification) -> dict[str, scoring.Contribution]:
    """Each code's trail line, by code, from the independent lines and every path scored."""
    found = [*result.contributions]
    for path in (result.selected_path, result.alternate_path):
        if path is not None:
            found.extend(path.contributions)
    return {line.label: line for line in found if line.kind is scoring.LineKind.CODE}


def _rarity_moved(
    ref: reference.Reference,
    result: classify.Classification,
    committed: svcv4_classification_pb2.Svcv4Classification | None,
) -> dict[str, str]:
    """Why each code SM4 conditions on POP_FRQ cannot carry its annotation, where POP_FRQ moved under it.

    Whether a clinical code applies at all rests on POP_FRQ's points, so its standing, not scored in particular, was
    written against the POP_FRQ it sat beside.
    """
    if committed is None:
        return {}
    was = next((code.points for code in committed.codes if code.code == 'POP_FRQ'), '')
    line = _trail(result).get('POP_FRQ')
    now = '' if line is None else _decimal(line.points)
    if was == now:
        return {}
    reason = f'POP_FRQ, which decides whether it applies, moved from {was or "no points"!r} to {now or "no points"!r}'
    return dict.fromkeys(ref.clinical_pop_frq_precondition.conditioned_codes, reason)


def _independent_releases(inputs: Inputs) -> dict[str, tuple[provenance.Release, ...]]:
    """The releases each independent code was read at, as the door that scored it carries them."""
    codes = inputs.independent_codes
    if codes is None or isinstance(codes, Mapping):
        return {}
    return {entry.code: entry.releases for entry in codes}


@dataclasses.dataclass(frozen=True)
class _PathContext:
    """What a path code's annotation was written against: the arm taken, the axes it was scaled by, the releases."""

    label: str
    mechanism: str
    exon_relevance: str
    releases: tuple[tuple[str, str], ...]


def _path_context(
    routes: Sequence[svcv4_classification_pb2.Svcv4Classification.Route],
    paths: Sequence[svcv4_classification_pb2.Svcv4Classification.Path],
) -> dict[str, _PathContext]:
    """Each path's context, by family."""
    labels = {route.family: route.label for route in routes}
    return {
        path.family: _PathContext(
            label=labels.get(path.family, ''),
            mechanism=path.mechanism,
            exon_relevance=path.exon_relevance,
            releases=tuple((release.source, release.dataset_version) for release in path.releases),
        )
        for path in paths
    }


def _moved(family: str, before: Mapping[str, _PathContext], now: Mapping[str, _PathContext]) -> str:
    """Why a path code's committed annotation no longer fits the path, or '' where the path is the same."""
    if family == '' or family not in before or before[family] == now[family]:
        return ''
    old, new = before[family], now[family]
    if old.label != new.label:
        return f'its path changed from {old.label!r} to {new.label!r}'
    if old.mechanism != new.mechanism:
        return f'its path is now scaled by mechanism {new.mechanism!r}, not {old.mechanism!r}'
    if old.exon_relevance != new.exon_relevance:
        return f'its path is now scaled by exon relevance {new.exon_relevance!r}, not {old.exon_relevance!r}'
    return 'the releases its path was read at changed'


def _framework(ref: reference.Reference) -> svcv4_classification_pb2.Svcv4Classification.Framework:
    return Record.Framework(
        name=meta.FRAMEWORK.framework,
        status=meta.FRAMEWORK.status,
        citations_repository=ref.cited_documents.repository,
        citations_revision=ref.cited_documents.revision,
        usage=meta.FRAMEWORK.usage,
    )


def _routing(
    annotation: svcv4_classification_pb2.Svcv4Classification.Routing | None,
    committed: svcv4_classification_pb2.Svcv4Classification | None,
    inputs: Inputs,
    routes: Sequence[scoring.PathResult],
) -> svcv4_classification_pb2.Svcv4Classification.Routing:
    labels = [path.label for path in routes]
    if annotation is None:
        if committed is None:
            raise AnnotationError('the routing has no annotation: state the variant, the entity and the rationale')
        annotation = committed.routing
        moved = []
        if annotation.consequence != inputs.consequence:
            moved.append('consequence')
        if [route.label for route in annotation.routes] != labels:
            moved.append('paths taken')
        if annotation.entity.gate_level != inputs.gate_level:
            moved.append('gate level')
        if moved:
            raise AnnotationError(
                f'the routing is not restated, and its {" and ".join(moved)} changed; restate the routing, whose '
                'rationale and entity were written for the old one'
            )
    elif annotation.reviewed or annotation.note:
        raise AnnotationError("the routing's annotation sets a curator's judgement; leave `reviewed` and `note` unset")
    elif annotation.routes or annotation.consequence:
        raise AnnotationError('the routing states its routes and consequence from the inputs; leave both unset')
    elif annotation.entity.gate_level and annotation.entity.gate_level != inputs.gate_level:
        raise AnnotationError(
            f"the routing's entity states gate level {reference.gate_level_name(annotation.entity.gate_level)}, "
            f'and the inputs {reference.gate_level_name(inputs.gate_level)}; the inputs state it, so leave it unset'
        )
    routing = Record.Routing(
        variant=annotation.variant,
        consequence=inputs.consequence,
        entity=annotation.entity,
        routes=[Record.Route(family=_family(path), label=path.label) for path in routes],
        rationale=annotation.rationale,
    )
    routing.entity.gate_level = inputs.gate_level
    return routing


def _code(
    ref: reference.Reference,
    code: str,
    path_family: str,
    lines: Mapping[str, scoring.Contribution],
    releases: Sequence[provenance.Release],
    annotation: svcv4_classification_pb2.Svcv4Classification.Code | None,
    committed: svcv4_classification_pb2.Svcv4Classification.Code | None,
    *,
    moved: str,
) -> svcv4_classification_pb2.Svcv4Classification.Code:
    spec = ref.code(code)
    line = lines.get(code)
    source = annotation if annotation is not None else committed
    if source is None:
        raise AnnotationError(
            f'{code} has no annotation: state its status, and for a scored code its rationale and confidence'
        )
    if annotation is not None:
        _check_annotation(code, annotation)
    built = Record.Code(code=code, family=spec.family, title=spec.title, path_family=path_family)
    informative_zero = source.status == _SCORED and path_family != '' and spec.concept == 'INF'
    _set_computed(built, line, informative_zero=informative_zero)
    built.releases.extend(_release(release) for release in releases)
    _copy_annotation(annotation if annotation is not None else _carried(code, built, source, moved), built)
    _check_status(ref, code, spec, built, line is not None)
    _price_cells(ref, code, built)
    return built


def _set_computed(
    built: svcv4_classification_pb2.Svcv4Classification.Code,
    line: scoring.Contribution | None,
    *,
    informative_zero: bool,
) -> None:
    """The points, raw points, adjustment and basis the tally gives a code.

    A path adds no line for an informative-variant sum of zero, so a path's INF code annotated scored with no line
    reads as zero.
    """
    if line is not None:
        built.points = _decimal(line.points)
        built.raw_points = _decimal(line.raw_points)
        built.adjustment = line.note
        built.basis = line.basis
    elif informative_zero:
        built.points = built.raw_points = _decimal(decimal.Decimal(0))


def _copy_annotation(
    source: svcv4_classification_pb2.Svcv4Classification.Code, target: svcv4_classification_pb2.Svcv4Classification.Code
) -> None:
    """Copy the annotation fields `source` sets onto `target`."""
    for name in _CODE_ANNOTATION:
        field = source.DESCRIPTOR.fields_by_name[name]
        if field.is_repeated:
            getattr(target, name).extend(getattr(source, name))
        elif field.message_type is not None:
            if source.HasField(name):
                getattr(target, name).CopyFrom(getattr(source, name))
        else:
            setattr(target, name, getattr(source, name))


def _check_annotation(code: str, annotation: svcv4_classification_pb2.Svcv4Classification.Code) -> None:
    """Refuse an annotation that sets what is the library's to compute or a curator's to judge."""
    if annotation.code and annotation.code != code:
        raise AnnotationError(f'the annotation filed under {code} names {annotation.code}')
    for name in (*_CODE_GUARDS, 'family', 'title', *_CODE_COMPUTED):
        if getattr(annotation, name):
            whose = "a curator's judgement" if name in _CODE_GUARDS else 'computed from the tally'
            raise AnnotationError(f"{code}'s annotation sets {name}, which is {whose}; leave it unset")
    for cell in annotation.cells:
        for field, _ in cell.ListFields():
            if field.name not in _CELL_ANNOTATION:
                raise AnnotationError(
                    f"{code}'s cell {cell.cell_id} sets {field.name}, which the library reads off the reference; "
                    'name the cell and the count only'
                )


def _carried(
    code: str,
    built: svcv4_classification_pb2.Svcv4Classification.Code,
    committed: svcv4_classification_pb2.Svcv4Classification.Code,
    moved: str,
) -> svcv4_classification_pb2.Svcv4Classification.Code:
    """The committed annotation of a code the agent did not restate, while what it was written against is unchanged."""
    if moved:
        raise AnnotationError(f'{code} is not restated, and {moved}; restate its annotation for the path it is on now')
    if list(committed.releases) != list(built.releases):
        raise AnnotationError(
            f'{code} is not restated, and the releases it was read at changed; restate its annotation for them'
        )
    for name in ('points', 'raw_points', 'adjustment', 'basis'):
        if getattr(committed, name) != getattr(built, name):
            raise AnnotationError(
                f'{code} is not restated, and its {name} changed from {getattr(committed, name)!r} to '
                f'{getattr(built, name)!r}; restate its annotation for what the tally now gives it'
            )
    carried = Record.Code()
    _copy_annotation(committed, carried)
    for cell in carried.cells:
        cell.ClearField('description')
        cell.ClearField('points_each')
    return carried


def _check_status(
    ref: reference.Reference,
    code: str,
    spec: reference.CodeSpec,
    built: svcv4_classification_pb2.Svcv4Classification.Code,
    in_trail: bool,
) -> None:
    """Refuse a status the tally contradicts: scored exactly when the tally carries the code."""
    scored = built.points != ''
    if built.status == _SCORED and not scored:
        raise AnnotationError(
            f'{code} is annotated scored, and the tally carries no line for it: pass it to the library, or state '
            'why it was not scored'
        )
    if built.status != _SCORED and in_trail:
        raise AnnotationError(
            f'{code} reaches the tally at {built.points} and is annotated as not scored; annotate it as scored'
        )
    if built.status == _SCORED and spec.family in ref.independent_families and not built.cells:
        raise AnnotationError(f'{code} is scored per observation; name the cells it was priced from')


def _price_cells(ref: reference.Reference, code: str, built: svcv4_classification_pb2.Svcv4Classification.Code) -> None:
    """Fill each cell's description and value, and refuse cells whose priced sum is not the code's raw points."""
    if built.cells and built.path_family != '':
        raise AnnotationError(f'{code} is priced on a path, from a tier: name its cell in `decision`, not in `cells`')
    if built.decision and built.path_family == '':
        raise AnnotationError(f'{code} is priced per observation: name the rows it was priced from in `cells`')
    nearest = built.nearest_alternative.cell_id if built.HasField('nearest_alternative') else ''
    if nearest and (not nearest.startswith(f'{code}.') or not _is_cell(ref, nearest)):
        raise AnnotationError(f"{code}'s nearest alternative {nearest} is no row of {code}'s tables")
    repeated = sorted(
        {cell.cell_id for cell in built.cells if [c.cell_id for c in built.cells].count(cell.cell_id) > 1}
    )
    if repeated:
        raise AnnotationError(f'{code} lists {", ".join(repeated)} more than once; state each row once, with its count')
    unvalued = observations.unvalued_cells(ref)
    for cell in built.cells:
        if not cell.cell_id.startswith(f'{code}.'):
            raise AnnotationError(f"{code}'s cell {cell.cell_id} is another code's")
        if cell.count < 1:
            raise AnnotationError(f"{code}'s cell {cell.cell_id} counts {cell.count} observations; count at least one")
        try:
            cell.description = observations.description(ref, cell.cell_id)
        except observations.UnknownCellError as e:
            raise AnnotationError(f"{code}'s cell {cell.cell_id} is no row of the framework's tables") from e
        if cell.cell_id not in unvalued:
            cell.points_each = _decimal(observations.points_for(ref, cell.cell_id))
    if not built.cells:
        return
    if built.points == '':
        if any(cell.cell_id not in unvalued for cell in built.cells):
            raise AnnotationError(
                f'{code} is not scored, so its cells can only be rows the framework declines to value'
            )
        return
    declined = sorted(cell.cell_id for cell in built.cells if cell.cell_id in unvalued)
    if declined:
        raise AnnotationError(
            f'{code} is scored, and the framework declines to value {", ".join(declined)}: a determination with no '
            'value scores nothing, so mark the code no data and name the row among its cells'
        )
    priced = observations.total(ref, {cell.cell_id: cell.count for cell in built.cells})
    if priced != decimal.Decimal(built.raw_points):
        raise AnnotationError(
            f"{code}'s cells price to {_decimal(priced)}, and it reached the tally at {built.raw_points}; the cells "
            'and the points have to be the same observations'
        )


def _is_cell(ref: reference.Reference, cell_id: str) -> bool:
    """Whether the framework's tables name the row, valued or not."""
    try:
        observations.description(ref, cell_id)
    except observations.UnknownCellError:
        return False
    return True


def _path(
    inputs: Inputs, path: scoring.PathResult, selected: bool
) -> svcv4_classification_pb2.Svcv4Classification.Path:
    mechanism, exon = _axes(inputs, path)
    return Record.Path(
        family=_family(path),
        selected=selected,
        total=_decimal(path.total),
        multiplier=_decimal(path.multiplier),
        mechanism=mechanism,
        exon_relevance=exon,
        releases=[_release(release) for release in _path_releases(inputs, path)],
    )


def _path_evidence(inputs: Inputs, path: scoring.PathResult) -> object:
    """The evidence family the path was built from: a missense variant's amino-acid or splice half."""
    evidence = inputs.evidence
    if isinstance(evidence, builders.MissensePaths):
        return evidence.amino_acid if _family(path) == 'MIS' else evidence.splice
    return evidence


def _axes(inputs: Inputs, path: scoring.PathResult) -> tuple[str, str]:
    """The mechanism level and the exon-relevance call the path was scaled by, each empty where it was not."""
    evidence = _path_evidence(inputs, path)
    mechanism = getattr(evidence, 'mechanism', None)
    exon = getattr(evidence, 'exon', None)
    mechanism_text = mechanism.value if isinstance(mechanism, scoring.MechanismLevel) else ''
    if isinstance(exon, scoring.ExonRelevanceWaiver):
        exon_text = exon.describe()
    elif isinstance(exon, scoring.ExonRelevance):
        exon_text = exon.value
    else:
        exon_text = ''
    return mechanism_text, exon_text


def _path_releases(inputs: Inputs, path: scoring.PathResult) -> tuple[provenance.Release, ...]:
    evidence = _path_evidence(inputs, path)
    releases = getattr(evidence, 'releases', ())
    return tuple(releases)


def _release(release: provenance.Release) -> svcv4_classification_pb2.Svcv4Classification.Release:
    return Record.Release(source=release.source, dataset_version=release.dataset_version)


_LINE_KINDS: Mapping[scoring.LineKind, svcv4_classification_pb2.Svcv4Classification.Line.Kind] = {
    scoring.LineKind.CAP: Record.Line.KIND_CAP,
    scoring.LineKind.AWARD: Record.Line.KIND_AWARD,
}


def _lines(result: classify.Classification) -> list[svcv4_classification_pb2.Svcv4Classification.Line]:
    """The trail's lines no code carries: caps on a subtotal, and awards such as the critical residue."""
    found = [(line, '') for line in result.contributions]
    for path in (result.selected_path, result.alternate_path):
        if path is not None:
            found.extend((line, _family(path)) for line in path.contributions)
    return [
        Record.Line(
            kind=_LINE_KINDS[line.kind],
            label=line.label,
            points=_decimal(line.points),
            raw_points=_decimal(line.raw_points),
            note=line.note,
            path_family=family,
        )
        for line, family in found
        if line.kind in _LINE_KINDS
    ]


def _class(ref: reference.Reference, name: str, gate_level: gene_disease_pb2.GateLevel) -> svcv4_pb2.Classification:
    """A class the library states as a string, in the class vocabulary."""
    if name in _CLASSES:
        return _CLASSES[name]
    terminal = ref.gate[gate_level].result
    if terminal is not None and name == terminal and gate_level in _TERMINAL:
        return _TERMINAL[gate_level]
    raise ValueError(f'the library states a class {name!r} the class vocabulary names no member for')


def _tally(
    ref: reference.Reference, result: classify.Classification, gate_level: gene_disease_pb2.GateLevel
) -> svcv4_classification_pb2.Svcv4Classification.Tally:
    return Record.Tally(
        total=_decimal(result.total),
        band=_CLASSES[result.band],
        vus_subband=result.vus_subband or '',
        final_class=_class(ref, result.final_class, gate_level),
        gate_capped=result.gate_capped,
    )


def _points_by_code(result: classify.Classification) -> dict[str, decimal.Decimal]:
    return {label: line.points for label, line in _trail(result).items()}


def _alternative(
    ref: reference.Reference,
    main: classify.Classification,
    alternative: Alternative,
    admitted: Sequence[tuple[str, str]],
) -> svcv4_classification_pb2.Svcv4Classification.Alternative:
    """An alternative's tally, and the codes of the record whose points it moves.

    An alternative on another arm moves codes the record does not list; the record names the ones it shows.
    """
    result = alternative.inputs.classify(ref)
    before, after = _points_by_code(main), _points_by_code(result)
    changed = {code for code in before.keys() | after.keys() if before.get(code) != after.get(code)}
    return Record.Alternative(
        assumption=alternative.assumption,
        codes=[code for code, _ in admitted if code in changed],
        tally=_tally(ref, result, alternative.inputs.gate_level),
    )


def _bands(ref: reference.Reference) -> list[svcv4_classification_pb2.Svcv4Classification.Band]:
    """The bands benign to pathogenic, a VUS total's sub-bands in the VUS band's place."""
    bands = []
    for band in ref.bands:
        tiles = ref.vus_subbands if band.code == 'VUS' else (band,)
        bands.extend(
            Record.Band(
                name=tile.code,
                classification=_CLASSES[band.code],
                lower='' if tile.lower is None else _decimal(tile.lower),
                lower_inclusive=tile.lower_inclusive,
                upper='' if tile.upper is None else _decimal(tile.upper),
                upper_inclusive=tile.upper_inclusive,
            )
            for tile in tiles
        )
    return bands


def _check_sum(payload: svcv4_classification_pb2.Svcv4Classification, result: classify.Classification) -> None:
    """Hold the counted codes and lines to the total, the invariant the ruler draws."""
    selected = {path.family for path in payload.paths if path.selected}
    counted = sum(
        (
            decimal.Decimal(code.points)
            for code in payload.codes
            if code.points and (code.path_family == '' or code.path_family in selected)
        ),
        decimal.Decimal(0),
    )
    counted += sum(
        (
            decimal.Decimal(line.points)
            for line in payload.lines
            if line.path_family == '' or line.path_family in selected
        ),
        decimal.Decimal(0),
    )
    if counted != result.total:
        raise ValueError(
            f'the counted codes and lines sum to {_decimal(counted)}, and the tally is {_decimal(result.total)}'
        )
