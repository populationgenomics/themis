"""Tests for the score-to-bin mappings and single-predictor enforcement."""

from __future__ import annotations

import decimal
from collections.abc import Callable

import pytest

from themis.svcv4 import predictors

D = decimal.Decimal

# Each implemented predictor's published score range, stated here from the source rather than read
# off the module, so a mis-transcribed guard fails instead of agreeing with itself. AlphaMissense's
# `am_pathogenicity` is a probability; BayesDel's range is the one its author publishes.
_RANGES: dict[Callable[[decimal.Decimal], decimal.Decimal], tuple[decimal.Decimal, decimal.Decimal]] = {
    predictors.alphamissense_points: (D('0'), D('1')),
    predictors.bayesdel_points: (D('-1.29334'), D('0.75731')),
}


@pytest.mark.parametrize(
    ('score', 'expected'),
    [
        ('0.0', '-3.0'),
        ('0.070', '-3.0'),  # inclusive upper edge of the -3 bin
        ('0.08', '-2.0'),
        ('0.15', '-1.0'),
        ('0.169', '-1.0'),
        ('0.170', '0.0'),  # just past the -1 bin
        ('0.5', '0.0'),
        ('0.791', '0.0'),
        ('0.85', '1.0'),
        ('0.95', '2.0'),
        ('0.98', '3.0'),
        ('0.989', '3.0'),
        ('0.990', '4.0'),  # inclusive lower edge of the +4 bin
        ('1.0', '4.0'),
    ],
)
def test_alphamissense_bins(score: str, expected: str) -> None:
    assert predictors.alphamissense_points(D(score)) == D(expected)


@pytest.mark.parametrize(
    ('score', 'expected'),
    [
        ('-1.29334', '-3.0'),  # the bottom of the score range
        ('-0.520', '-3.0'),  # inclusive upper edge of the -3 bin
        ('-0.5195', '-2.0'),  # inside the -0.520 / -0.519 rounding gap: closed to the -3 bin's edge
        ('-0.519', '-2.0'),  # the -2 cell's lower bound, read monotonically (printed "-0.0519")
        ('-0.400', '-2.0'),
        ('-0.360', '-2.0'),
        ('-0.359', '-1.0'),
        ('-0.180', '-1.0'),
        ('-0.179', '0.0'),
        ('0.0', '0.0'),
        ('0.129', '0.0'),
        ('0.130', '1.0'),
        ('0.269', '1.0'),
        ('0.270', '2.0'),
        ('0.409', '2.0'),
        ('0.410', '3.0'),
        ('0.499', '3.0'),
        ('0.4995', '4.0'),  # inside the +0.499 / +0.50 rounding gap: closed the same way
        ('0.50', '4.0'),
        ('0.75731', '4.0'),  # the top of the score range
    ],
)
def test_bayesdel_bins(score: str, expected: str) -> None:
    assert predictors.bayesdel_points(D(score)) == D(expected)


@pytest.mark.parametrize('score', ['-0.1', '1.1'])
def test_alphamissense_rejects_out_of_range(score: str) -> None:
    with pytest.raises(ValueError, match='in \\[0, 1\\]'):
        predictors.alphamissense_points(D(score))


@pytest.mark.parametrize('score', ['-1.29335', '0.75732', '0.9'])
def test_bayesdel_rejects_a_score_off_its_scale(score: str) -> None:
    """0.9 is the case that matters: a valid AlphaMissense score, off BayesDel's scale entirely."""
    with pytest.raises(ValueError, match='BayesDel_noAF'):
        predictors.bayesdel_points(D(score))


@pytest.mark.parametrize('table', list(_RANGES))
def test_the_published_range_is_admitted_whole(
    table: Callable[[decimal.Decimal], decimal.Decimal],
) -> None:
    """A guard narrower than the predictor's scale refuses real scores; wider, it admits wrong ones."""
    low, high = _RANGES[table]
    step = D('0.00001')
    assert table(low) == D('-3.0')
    assert table(high) == D('4.0')
    for outside in (low - step, high + step):
        with pytest.raises(ValueError, match='must be in'):
            table(outside)


@pytest.mark.parametrize('table', list(_RANGES))
def test_the_bins_partition_the_score_range(
    table: Callable[[decimal.Decimal], decimal.Decimal],
) -> None:
    """No gap and no overlap: sweeping the range yields each point value as one ascending run.

    Stronger than the per-boundary cases, and what the rounding-gap closure exists for — a gap would
    show up as a raise mid-range, an overlap or an out-of-order cell as a repeated or descending
    value.
    """
    low, high = _RANGES[table]
    step = D('0.0001')
    runs: list[decimal.Decimal] = []
    score = low
    while score <= high:
        points = table(score)  # in range, so a score belonging to no bin would raise here
        if not runs or points != runs[-1]:
            runs.append(points)
        score += step
    assert runs == sorted(runs)
    assert len(runs) == len(set(runs))
    assert runs[0] == D('-3.0')
    assert runs[-1] == D('4.0')


@pytest.mark.parametrize(
    ('predictor', 'score', 'expected'),
    [
        (predictors.Predictor.ALPHAMISSENSE, '0.995', '4.0'),
        (predictors.Predictor.BAYESDEL, '0.6', '4.0'),
    ],
)
def test_predictor_points_dispatches_every_implemented_predictor(
    predictor: predictors.Predictor, score: str, expected: str
) -> None:
    assert predictors.implements(predictor)
    assert predictors.predictor_points(predictor, D(score)) == D(expected)


@pytest.mark.parametrize(
    'predictor',
    [p for p in predictors.Predictor if p not in (predictors.Predictor.ALPHAMISSENSE, predictors.Predictor.BAYESDEL)],
)
def test_predictor_points_refuses_a_predictor_with_no_table(predictor: predictors.Predictor) -> None:
    """`implements` is what the policy loader gates on, so the two must not disagree."""
    assert not predictors.implements(predictor)
    with pytest.raises(NotImplementedError):
        predictors.predictor_points(predictor, D('0.9'))


@pytest.mark.parametrize(
    ('row', 'bound', 'expected'),
    [
        pytest.param(row, bound, points, id=f'{row.score}-{bound}')
        for row in (predictors._ALPHAMISSENSE, predictors._BAYESDEL)
        for bound, points in row.thresholds
    ],
)
def test_a_bin_bound_supplied_as_a_float_bins_where_the_table_says(
    row: predictors._Row, bound: decimal.Decimal, expected: decimal.Decimal
) -> None:
    """A float is the caller's normal case: the upstreams serve JSON numbers.

    A bound is where it can go wrong and nothing raises — the bin is inclusive of its upper bound, so
    a float carrying the binary expansion of that bound lands in the next bin up, toward pathogenic.
    """
    assert row.points(float(bound)) == expected


@pytest.mark.parametrize('row', [predictors._ALPHAMISSENSE, predictors._BAYESDEL])
def test_the_range_endpoints_as_floats_are_in_range(row: predictors._Row) -> None:
    """A published endpoint is a score the predictor can return, so the guard must admit it.

    `decimal.Decimal(0.75731)` exceeds BayesDel's published high, so converting directly rejected a
    top-of-range score outright rather than binning it.
    """
    assert row.points(float(row.low)) == row.thresholds[0][1]
    assert row.points(float(row.high)) == row.top


@pytest.mark.parametrize('score', [float('nan'), float('inf'), 'not-a-score'])
def test_a_score_that_is_not_a_finite_number_fails_loud(score: predictors.Score) -> None:
    """A NaN compares false against both range bounds, so an unguarded one reads as out of range."""
    with pytest.raises(ValueError, match=r'not a number|must be finite'):
        predictors.alphamissense_points(score)


def test_a_decimal_the_caller_built_from_a_float_is_refused() -> None:
    """The one form the conversion error survives in: the expansion is already baked in on arrival.

    Binning it would answer 1.0 where the table says 0.0, silently — so it raises instead. RUF032
    catches this spelling only on a literal; the caller's is `Decimal(score)` over a float variable.
    """
    with pytest.raises(ValueError, match='significant digits'):
        predictors.alphamissense_points(decimal.Decimal(0.791))  # noqa: RUF032 — the mistake under test
