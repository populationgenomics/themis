"""The classification widget's payload: every number is the library's, every admitted code is accounted for."""

from __future__ import annotations

import dataclasses
import decimal
from collections.abc import Callable

import pytest

from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2
from themis.svcv4 import builders, classify, data, provenance, reference, scoring, splice_tree, widget
from themis.svcv4.models import svcv4_pb2
from themis.svcv4.tests import widget_cases
from themis.widgets import ownership
from themis.widgets.models import svcv4_classification_pb2

Record = svcv4_classification_pb2.Svcv4Classification
D = decimal.Decimal
SCORED = svcv4_pb2.ASSESSMENT_STATUS_SCORED
NO_DATA = svcv4_pb2.ASSESSMENT_STATUS_NO_DATA
_missense = widget_cases.missense
_routing = widget_cases.routing
_annotations = widget_cases.code_annotations
_built = widget_cases.built
_CELLS = widget_cases.CELLS


@pytest.fixture(scope='module')
def ref() -> reference.Reference:
    return data.load_reference()


def test_every_number_is_the_library_s(ref: reference.Reference) -> None:
    inputs = _missense()
    payload = _built(ref, inputs)
    result = inputs.classify(ref)
    assert D(payload.tally.total) == result.total
    assert payload.tally.final_class == svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC
    assert payload.classification == payload.tally.final_class
    by_code = {code.code: code for code in payload.codes}
    assert D(by_code['CLN_DNV'].points) == D('2.0')
    [selected] = [path for path in payload.paths if path.selected]
    assert D(selected.total) == result.selected_path.total  # pyright: ignore[reportOptionalMemberAccess]
    assert [route.label for route in payload.routing.routes] == ['amino-acid (MIS_)', 'splice blue (SPL_)']
    assert payload.routing.entity.gate_level == gene_disease_pb2.GATE_LEVEL_DEFINITIVE


def test_the_codes_are_the_ones_the_paths_taken_admit_in_the_reference_s_order(ref: reference.Reference) -> None:
    payload = _built(ref, _missense())
    names = [code.code for code in payload.codes]
    assert names == [code for code in ref.codes if code in set(names)]
    assert {code.path_family for code in payload.codes if code.code.startswith('SPL_')} == {'SPL'}
    without_splice = _built(ref, _missense(splice=False))
    assert not [code for code in without_splice.codes if code.family == 'SPL']
    nonsense = widget.Inputs(
        consequence=evidence_pb2.CONSEQUENCE_NONSENSE,
        evidence=builders.NullEvidence(
            nul_prd=D('6'), mechanism=scoring.MechanismLevel.ESTABLISHED, exon=scoring.ExonRelevance.ALL
        ),
        independent_codes=[classify.IndependentCode('POP_FRQ', D('0.0'))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    null_arm = _built(ref, nonsense)
    assert {code.family for code in null_arm.codes} & {'CDS', 'MIS', 'SPL'} == set()
    assert null_arm.paths[0].mechanism == 'Established'


def test_a_code_left_without_a_status_is_refused(ref: reference.Reference) -> None:
    inputs = _missense()
    codes = _annotations(ref, inputs)
    del codes['CLN_CCS']
    with pytest.raises(widget.AnnotationError, match='CLN_CCS has no annotation'):
        widget.build(ref, inputs, routing=_routing(), codes=codes)


@pytest.mark.parametrize(
    ('code', 'annotation', 'reason'),
    [
        (
            'CLN_AFF',
            Record.Code(status=SCORED, rationale='r', confidence=svcv4_pb2.CONFIDENCE_SETTLED),
            'the tally carries no line',
        ),
        ('CLN_DNV', Record.Code(status=NO_DATA, status_reason='none'), 'annotate it as scored'),
        (
            'MIS_FXN',
            Record.Code(status=SCORED, rationale='r', confidence=svcv4_pb2.CONFIDENCE_SETTLED),
            'the tally carries no line',
        ),
        (
            'CLN_DNV',
            Record.Code(status=SCORED, points='9', rationale='r', confidence=svcv4_pb2.CONFIDENCE_SETTLED),
            'computed from the tally',
        ),
        (
            'CLN_DNV',
            Record.Code(status=SCORED, reviewed=True, rationale='r', confidence=svcv4_pb2.CONFIDENCE_SETTLED),
            "curator's judgement",
        ),
        (
            'CLN_DNV',
            Record.Code(status=SCORED, rationale='r', confidence=svcv4_pb2.CONFIDENCE_SETTLED),
            'name the cells',
        ),
        (
            'CLN_DNV',
            Record.Code(
                status=SCORED,
                rationale='r',
                confidence=svcv4_pb2.CONFIDENCE_SETTLED,
                cells=[Record.Cell(cell_id='CLN_DNV.specific.confirmed', count=1)],
            ),
            'price to 7.0',
        ),
        (
            'CLN_DNV',
            Record.Code(
                status=SCORED,
                rationale='r',
                confidence=svcv4_pb2.CONFIDENCE_SETTLED,
                cells=[Record.Cell(cell_id='LOC_PHE.yield.ge_82', count=1)],
            ),
            "another code's",
        ),
        ('XYZ_ABC', Record.Code(status=NO_DATA, status_reason='none'), 'not a code the paths taken admit'),
    ],
)
def test_an_annotation_the_tally_contradicts_is_refused(
    ref: reference.Reference, code: str, annotation: svcv4_classification_pb2.Svcv4Classification.Code, reason: str
) -> None:
    inputs = _missense()
    codes = _annotations(ref, inputs)
    codes[code] = annotation
    with pytest.raises(widget.AnnotationError, match=reason):
        widget.build(ref, inputs, routing=_routing(), codes=codes)


def test_a_path_s_informative_code_scores_zero_with_no_line(ref: reference.Reference) -> None:
    inputs = _missense()
    codes = _annotations(ref, inputs)
    codes['MIS_INF'] = Record.Code(
        status=SCORED, rationale='no informative variant', confidence=svcv4_pb2.CONFIDENCE_SETTLED
    )
    payload = widget.build(ref, inputs, routing=_routing(), codes=codes)
    [mis_inf] = [code for code in payload.codes if code.code == 'MIS_INF']
    assert D(mis_inf.points) == 0


def test_an_unrestated_code_keeps_its_annotation_while_the_tally_leaves_it_alone(ref: reference.Reference) -> None:
    first = _built(ref, _missense())
    later = _missense(dnv='7.0')
    restated = {
        'CLN_DNV': Record.Code(
            status=SCORED,
            rationale='parentage confirmed',
            confidence=svcv4_pb2.CONFIDENCE_SETTLED,
            cells=[Record.Cell(cell_id='CLN_DNV.specific.confirmed', count=1)],
        )
    }
    second = widget.build(ref, later, codes=restated, committed=first)
    before = {code.code: code for code in first.codes}
    after = {code.code: code for code in second.codes}
    assert after['POP_FRQ'] == before['POP_FRQ']
    assert after['CLN_DNV'].rationale == 'parentage confirmed'
    assert second.routing == first.routing
    with pytest.raises(widget.AnnotationError, match='CLN_DNV is not restated, and its points changed'):
        widget.build(ref, later, committed=first)


def test_a_curator_s_tick_survives_on_the_codes_the_rebuild_left_alone(ref: reference.Reference) -> None:
    first = _built(ref, _missense())
    for code in first.codes:
        if code.code in ('POP_FRQ', 'CLN_DNV'):
            code.reviewed = True
            code.note = 'looked at it'
    restated = {
        'CLN_DNV': Record.Code(
            status=SCORED,
            rationale='parentage confirmed',
            confidence=svcv4_pb2.CONFIDENCE_SETTLED,
            cells=[Record.Cell(cell_id='CLN_DNV.specific.confirmed', count=1)],
        )
    }
    second = widget.build(ref, _missense(dnv='7.0'), codes=restated, committed=first)
    carried, cleared = ownership.carry(first, second)
    assert isinstance(carried, Record)
    ticked = {code.code for code in carried.codes if code.reviewed}
    assert ticked == {'POP_FRQ'}
    assert {str(each).split(' ')[0] for each in cleared} >= {'codes[CLN_DNV].reviewed', 'codes[CLN_DNV].note'}


def test_the_record_reports_no_class_where_an_open_value_lands_elsewhere(ref: reference.Reference) -> None:
    inputs = _missense()
    confirmed = widget.Alternative('parentage confirmed', _missense(dnv='7.0'))
    open_payload = _built(ref, inputs, open_values=[confirmed])
    assert open_payload.classification == svcv4_pb2.CLASSIFICATION_NOT_ESTABLISHED
    assert list(open_payload.open_values[0].codes) == ['CLN_DNV']
    assert open_payload.open_values[0].tally.final_class == svcv4_pb2.CLASSIFICATION_PATHOGENIC
    agreeing = widget.Alternative('a zero predictor', dataclasses.replace(inputs, evidence=_missense().evidence))
    assert _built(ref, inputs, open_values=[agreeing]).classification == svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC
    sensitivity = _built(ref, inputs, sensitivity=[confirmed])
    assert sensitivity.classification == svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC


def test_a_terminal_gate_is_a_class_of_its_own(ref: reference.Reference) -> None:
    payload = _built(ref, _missense(gate=gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED))
    assert payload.tally.final_class == svcv4_pb2.CLASSIFICATION_VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE
    assert payload.tally.gate_capped


def test_a_cap_on_a_subtotal_is_a_line_that_keeps_the_sum(ref: reference.Reference) -> None:
    inputs = dataclasses.replace(
        _missense(),
        independent_codes=[
            classify.IndependentCode('POP_FRQ', D('0.0')),
            classify.IndependentCode('LOC_PHE', D('4.0')),
            classify.IndependentCode('LOC_SEG', D('2.0')),
        ],
    )
    cells = {**_CELLS, 'LOC_SEG': [('LOC_SEG.ad.het_affected', 2)]}
    payload = widget.build(ref, inputs, routing=_routing(), codes=_annotations(ref, inputs, cells))
    [cap] = [line for line in payload.lines if line.label == 'LOC combined cap']
    assert (D(cap.points), D(cap.raw_points)) == (D('-2.0'), D('6.0'))


def test_a_whole_gene_deletion_is_reached(ref: reference.Reference) -> None:
    inputs = widget.Inputs(
        consequence=evidence_pb2.CONSEQUENCE_EXON_DELETION,
        evidence=builders.NullEvidence(nul_prd=D('10'), mechanism=scoring.MechanismLevel.ESTABLISHED),
        independent_codes=[classify.IndependentCode('POP_FRQ', D('0.0'))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        whole_gene=True,
    )
    payload = _built(ref, inputs)
    assert payload.routing.routes[0].label == 'whole-gene (NUL_)'
    assert payload.paths[0].exon_relevance == ''


def test_a_decimal_is_fixed_point_text(ref: reference.Reference) -> None:
    inputs = dataclasses.replace(
        _missense(),
        independent_codes=[
            classify.IndependentCode('POP_FRQ', D('0.0')),
            classify.IndependentCode('CLN_DNV', D('1.4E+1')),
            classify.IndependentCode('LOC_PHE', D('4.0')),
        ],
    )
    cells = {**_CELLS, 'CLN_DNV': [('CLN_DNV.specific.confirmed', 2)]}
    payload = widget.build(ref, inputs, routing=_routing(), codes=_annotations(ref, inputs, cells))
    [dnv] = [code for code in payload.codes if code.code == 'CLN_DNV']
    assert dnv.points == '14'


def _yellow_or_orange(colour: splice_tree.SpliceColour) -> widget.Inputs:
    return dataclasses.replace(
        _missense(),
        evidence=builders.MissensePaths(
            amino_acid=builders.AminoAcidEvidence(mis_prd=D('1'), exon=scoring.ExonRelevance.ALL),
            splice=builders.SpliceEvidence(
                colour=colour,
                spl_prd=D('3'),
                mechanism=scoring.MechanismLevel.ESTABLISHED,
                exon=scoring.ExonRelevance.ALL,
            ),
        ),
    )


def test_a_path_code_is_restated_when_its_path_moves_though_its_points_do_not(ref: reference.Reference) -> None:
    yellow = _yellow_or_orange(splice_tree.SpliceColour.YELLOW)
    first = _built(ref, yellow)
    orange = _yellow_or_orange(splice_tree.SpliceColour.ORANGE)
    assert [code.points for code in _built(ref, orange).codes if code.code == 'SPL_PRD'] == [
        code.points for code in first.codes if code.code == 'SPL_PRD'
    ]
    with pytest.raises(widget.AnnotationError, match='SPL_PRD is not restated, and its path changed'):
        widget.build(ref, orange, routing=_routing(), committed=first)


def test_the_routing_is_restated_when_its_route_or_gate_moves(ref: reference.Reference) -> None:
    first = _built(ref, _missense())
    same = widget.build(ref, _missense(), committed=first)
    assert same.routing == first.routing
    with pytest.raises(widget.AnnotationError, match='gate level changed; restate the routing'):
        widget.build(ref, _missense(gate=gene_disease_pb2.GATE_LEVEL_STRONG), committed=first)
    with pytest.raises(widget.AnnotationError, match='paths taken changed; restate the routing'):
        widget.build(ref, _missense(splice=False), codes=_annotations(ref, _missense(splice=False)), committed=first)


def test_a_door_s_releases_reach_its_code_and_hold_its_annotation(ref: reference.Reference) -> None:
    release = provenance.Release(source='gnomAD GraphQL', dataset_version='gnomad_r4')

    def with_release(version: str) -> widget.Inputs:
        return dataclasses.replace(
            _missense(),
            independent_codes=[
                classify.IndependentCode(
                    'POP_FRQ', D('0.0'), releases=(dataclasses.replace(release, dataset_version=version),)
                ),
                classify.IndependentCode('CLN_DNV', D('2.0')),
                classify.IndependentCode('LOC_PHE', D('4.0')),
            ],
        )

    first = _built(ref, with_release('gnomad_r4'))
    [frequency] = [code for code in first.codes if code.code == 'POP_FRQ']
    assert [(each.source, each.dataset_version) for each in frequency.releases] == [('gnomAD GraphQL', 'gnomad_r4')]
    with pytest.raises(widget.AnnotationError, match='POP_FRQ is not restated, and the releases'):
        widget.build(ref, with_release('gnomad_r5'), committed=first)


def test_an_award_is_a_line_of_its_own_kind(ref: reference.Reference) -> None:
    inputs = dataclasses.replace(
        _missense(),
        evidence=builders.MissensePaths(
            amino_acid=builders.AminoAcidEvidence(
                mis_prd=D('1'), exon=scoring.ExonRelevance.ALL, critical_residue=D('1')
            ),
        ),
    )
    payload = _built(ref, inputs)
    [award] = [line for line in payload.lines if line.label == 'critical residue']
    assert award.kind == Record.Line.KIND_AWARD
    assert D(award.points) > 0


@pytest.mark.parametrize(
    ('code', 'cells', 'reason'),
    [
        ('MIS_PRD', [('MIS_PRD.x', 1)], 'name its cell in `decision`'),
        ('CLN_DNV', [('CLN_DNV.specific.unconfirmed', 0)], 'count at least one'),
        ('CLN_DNV', [('CLN_DNV.nonesuch', 1)], "no row of the framework's tables"),
    ],
)
def test_a_cell_the_code_cannot_be_priced_from_is_refused(
    ref: reference.Reference, code: str, cells: list[tuple[str, int]], reason: str
) -> None:
    inputs = _missense()
    annotations = _annotations(ref, inputs)
    annotations[code].ClearField('cells')
    annotations[code].cells.extend(Record.Cell(cell_id=cell, count=count) for cell, count in cells)
    with pytest.raises(widget.AnnotationError, match=reason):
        widget.build(ref, inputs, routing=_routing(), codes=annotations)


def test_an_alternative_names_only_the_codes_the_record_lists(ref: reference.Reference) -> None:
    null = widget.Inputs(
        consequence=evidence_pb2.CONSEQUENCE_NONSENSE,
        evidence=builders.NullEvidence(
            nul_prd=D('6'), mechanism=scoring.MechanismLevel.ESTABLISHED, exon=scoring.ExonRelevance.ALL
        ),
        independent_codes=[classify.IndependentCode('POP_FRQ', D('0.0'))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    escape = dataclasses.replace(
        null,
        evidence=builders.CodingEvidence(
            cds_prd=D('2'), mechanism=scoring.MechanismLevel.ESTABLISHED, exon=scoring.ExonRelevance.ALL
        ),
    )
    payload = _built(ref, null, open_values=[widget.Alternative('NMD escape', escape)])
    listed = {code.code for code in payload.codes}
    assert set(payload.open_values[0].codes) <= listed
    assert 'NUL_PRD' in payload.open_values[0].codes


def _add_duplicate_cell(annotation: svcv4_classification_pb2.Svcv4Classification.Code) -> None:
    annotation.cells.append(Record.Cell(cell_id='CLN_DNV.specific.unconfirmed', count=1))


def _add_decision(annotation: svcv4_classification_pb2.Svcv4Classification.Code) -> None:
    annotation.decision = 'specific, unconfirmed'


def _add_unknown_nearest(annotation: svcv4_classification_pb2.Svcv4Classification.Code) -> None:
    annotation.nearest_alternative.CopyFrom(Record.NearestAlternative(cell_id='NOPE.nope', description='d', reason='r'))


@pytest.mark.parametrize(
    ('change', 'reason'),
    [
        (_add_duplicate_cell, 'more than once'),
        (_add_decision, 'name the rows'),
        (_add_unknown_nearest, 'no row of'),
    ],
)
def test_an_annotation_that_misnames_its_rows_is_refused(
    ref: reference.Reference,
    change: Callable[[svcv4_classification_pb2.Svcv4Classification.Code], None],
    reason: str,
) -> None:
    inputs = _missense()
    annotations = _annotations(ref, inputs)
    change(annotations['CLN_DNV'])
    with pytest.raises(widget.AnnotationError, match=reason):
        widget.build(ref, inputs, routing=_routing(), codes=annotations)


def test_a_path_code_is_restated_when_its_path_s_releases_move(ref: reference.Reference) -> None:
    def read_at(version: str) -> widget.Inputs:
        release = provenance.Release(source='Ensembl VEP', dataset_version=version)
        return dataclasses.replace(
            _missense(splice=False),
            evidence=builders.MissensePaths(
                amino_acid=builders.AminoAcidEvidence(
                    mis_prd=D('1'), exon=scoring.ExonRelevance.ALL, releases=(release,)
                ),
            ),
        )

    first = _built(ref, read_at('114'))
    with pytest.raises(widget.AnnotationError, match='MIS_PRD is not restated, and the releases its path'):
        widget.build(ref, read_at('115'), committed=first)


def test_a_clinical_code_s_standing_is_restated_when_pop_frq_moves_under_it(ref: reference.Reference) -> None:
    first = _built(ref, _missense())
    rarer = dataclasses.replace(
        _missense(),
        independent_codes=[
            classify.IndependentCode('POP_FRQ', D('-1.0')),
            classify.IndependentCode('CLN_DNV', D('2.0')),
            classify.IndependentCode('LOC_PHE', D('4.0')),
        ],
    )
    frequency = Record.Code(
        status=SCORED,
        rationale='1.5 to 5 times the threshold',
        confidence=svcv4_pb2.CONFIDENCE_SETTLED,
        cells=[Record.Cell(cell_id='POP_FRQ.bin.1_5x_to_5x', count=1)],
    )
    with pytest.raises(widget.AnnotationError, match='CLN_AFF is not restated, and POP_FRQ, which decides'):
        widget.build(ref, rarer, codes={'POP_FRQ': frequency}, committed=first)
