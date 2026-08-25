"""Tests for OddsPath-to-FXN calibration and the animal-model table."""

from __future__ import annotations

import decimal

import pytest

from themis.svcv4 import functional, provenance, reference
from themis.svcv4.tests import responses

D = decimal.Decimal


@pytest.mark.parametrize(
    ('odds_path', 'expected'),
    [
        ('350', '8'),  # Pathogenic-Very-Strong
        ('18.7', '4'),  # Pathogenic-Strong, boundary inclusive
        ('20', '4'),
        ('4.33', '2'),  # Pathogenic-Moderate
        ('2.08', '1'),  # Pathogenic-Supporting
        ('1.5', '0'),  # indeterminate
        ('1', '0'),
        ('0.5', '0'),
        ('0.48', '-1'),  # Benign-Supporting (1:2.08 ~ 0.4808)
        ('0.05', '-4'),  # Benign-Strong (1:18.7 ~ 0.0535)
    ],
)
def test_oddspath_points(ref: reference.Reference, odds_path: str, expected: str) -> None:
    assert functional.oddspath_points(ref, D(odds_path)) == D(expected)


def test_oddspath_rejects_nonpositive(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='OddsPath'):
        functional.oddspath_points(ref, D('0'))


@pytest.mark.parametrize(
    ('consistency', 'same_inheritance', 'high_similarity', 'expected'),
    [
        (functional.PhenotypicConsistency.HIGH, True, True, '4'),
        (functional.PhenotypicConsistency.HIGH, False, True, '3'),
        (functional.PhenotypicConsistency.KEY_FEATURES, True, True, '2'),
        (functional.PhenotypicConsistency.KEY_FEATURES, False, True, '1'),
        (functional.PhenotypicConsistency.SIMILAR, True, True, '1'),
        (functional.PhenotypicConsistency.SIMILAR, False, True, '0'),
        (functional.PhenotypicConsistency.HIGH, True, False, '0'),  # similarity not high
        (functional.PhenotypicConsistency.NONE, True, True, '0'),
    ],
)
def test_animal_model_points(consistency, same_inheritance: bool, high_similarity: bool, expected: str) -> None:  # noqa: ANN001
    got = functional.animal_model_points(
        consistency, same_inheritance=same_inheritance, high_protein_similarity=high_similarity
    )
    assert got == D(expected)


def test_a_deposited_calibration_bins_to_its_tavtigian_step(ref: reference.Reference) -> None:
    scored = functional.fxn_from_mavedb(ref, responses.mavedb_response(), measures_disease_relevant_function=True)
    assert scored.support is functional.FxnSupport.CALIBRATED
    assert scored.points == functional.oddspath_points(ref, D('24.5'))
    assert 'PS3' in scored.derivation
    assert provenance.Release('MaveDB', 'MaveDB 2026-07-01') in scored.releases


def test_an_assay_measuring_another_function_scores_zero_with_the_judgement_named(
    ref: reference.Reference,
) -> None:
    # SM20 scores it 0.0 rather than removing the code, and the trail has to say why: two deposits
    # can both be right about different questions.
    scored = functional.fxn_from_mavedb(ref, responses.mavedb_response(), measures_disease_relevant_function=False)
    assert scored.points == D('0')
    assert scored.support is functional.FxnSupport.NOT_CONCORDANT
    assert 'mechanism' in scored.derivation


def test_a_deposit_carrying_no_calibration_determines_nothing(ref: reference.Reference) -> None:
    # FXN_ND: MaveDB runs no Brnich calibration and exposes no control counts, so nothing computes one.
    scored = functional.fxn_from_mavedb(
        ref, responses.mavedb_response(oddspath_ratio=None), measures_disease_relevant_function=True
    )
    assert scored.points is None
    assert scored.support is functional.FxnSupport.NO_CALIBRATION


def test_a_response_stating_no_provenance_is_refused(ref: reference.Reference) -> None:
    response = responses.mavedb_response()
    del response.provenance[:]
    with pytest.raises(ValueError, match='no provenance'):
        functional.fxn_from_mavedb(ref, response, measures_disease_relevant_function=True)


def _controls(
    ref: reference.Reference, *, result_range: functional.ControlRange, pathogenic: int, benign: int
) -> functional.Fxn:
    return functional.fxn_from_controls(
        ref,
        result_range=result_range,
        pathogenic_controls=pathogenic,
        benign_controls=benign,
        measures_disease_relevant_function=True,
        no_false_calls=True,
    )


def test_the_control_grids_award_each_direction_from_the_other_kinds_count(ref: reference.Reference) -> None:
    # SM20's stated asymmetry: benign controls drive the strength available for pathogenicity, and
    # pathogenic controls that for benignity. Reading one table for the other inverts it.
    pathogenic = _controls(ref, result_range=functional.ControlRange.PATHOGENIC, pathogenic=10, benign=10)
    benign = _controls(ref, result_range=functional.ControlRange.BENIGN, pathogenic=10, benign=10)
    assert pathogenic.points == D('3.0')
    assert benign.points == D('-3.0')
    assert 'Table 1' in pathogenic.derivation


def test_a_single_control_of_a_kind_is_worth_nothing(ref: reference.Reference) -> None:
    # SM20 §22: a single reference version of the protein does not meet the control standard, which
    # the grids state only through their all-zero lines.
    assert _controls(ref, result_range=functional.ControlRange.PATHOGENIC, pathogenic=10, benign=1).points == D('0.0')


def test_the_grids_never_reach_strong(ref: reference.Reference) -> None:
    """A property of the small-experiment regime: an assay needing ±4.0 takes the mathematical route."""
    for direction in functional.ControlRange:
        grid = ref.control_count_grid(direction.value)
        assert max(abs(points) for points in grid.cells.values()) < D('4.0')


def test_an_experiment_past_the_grids_extent_is_not_clamped_onto_the_last_row(ref: reference.Reference) -> None:
    # Clamping would award the strongest evidence the regime has to an experiment it does not cover.
    with pytest.raises(ValueError, match='reach no cell'):
        _controls(ref, result_range=functional.ControlRange.PATHOGENIC, pathogenic=40, benign=40)


def test_an_experiment_with_false_calls_is_outside_the_grids(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='no false positives'):
        functional.fxn_from_controls(
            ref,
            result_range=functional.ControlRange.PATHOGENIC,
            pathogenic_controls=10,
            benign_controls=10,
            measures_disease_relevant_function=True,
            no_false_calls=False,
        )


def test_a_control_experiment_measuring_another_function_scores_zero(ref: reference.Reference) -> None:
    scored = functional.fxn_from_controls(
        ref,
        result_range=functional.ControlRange.PATHOGENIC,
        pathogenic_controls=10,
        benign_controls=10,
        measures_disease_relevant_function=False,
        no_false_calls=True,
    )
    assert scored.points == D('0')
    assert scored.support is functional.FxnSupport.NOT_CONCORDANT
