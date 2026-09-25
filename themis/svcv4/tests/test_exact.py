"""Tests for the one conversion every figure from outside runs through."""

from __future__ import annotations

import decimal

import pytest

from themis.svcv4 import exact

D = decimal.Decimal


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (0.791, D('0.791')),  # a float on a bin edge is the edge, not its binary expansion
        (18.7, D('18.7')),
        (0.000118, D('0.000118')),
        (6, D('6')),
        ('0.0013', D('0.0013')),
        (D('0.85'), D('0.85')),
    ],
    ids=['float-bin-edge', 'float-bound', 'float-small', 'int', 'str', 'decimal'],
)
def test_a_figure_is_read_as_quoted_whatever_type_it_arrives_in(value: exact.Figure, expected: decimal.Decimal) -> None:
    assert exact.decimal_of(value, what='figure') == expected
    assert exact.decimal_of(value, what='figure').as_tuple() == expected.as_tuple()  # exact, not merely equal


def test_a_decimal_the_caller_built_from_a_float_fails_the_digit_bound() -> None:
    """The one form the conversion error survives in: the expansion is already baked in on arrival."""
    with pytest.raises(ValueError, match='significant digits'):
        exact.decimal_of(decimal.Decimal(0.791), what='figure')  # noqa: RUF032 — the mistake under test


@pytest.mark.parametrize('value', ['0.12345678901234567890', 123456789012345678901], ids=['str', 'int'])
def test_an_over_long_string_or_int_fails_the_digit_bound_without_the_float_remedy(value: exact.Figure) -> None:
    """The float remedy is wrong for a value that never was one; the error names the digit bound alone."""
    with pytest.raises(ValueError, match='quoted to that many') as failed:
        exact.decimal_of(value, what='figure')
    assert 'float' not in str(failed.value)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 'not-a-figure', True])
def test_a_value_that_is_not_a_finite_number_fails_loud(value: exact.Figure) -> None:
    with pytest.raises(ValueError, match=r'not a number|must be finite'):
        exact.decimal_of(value, what='figure')


def test_the_error_names_what_the_figure_is() -> None:
    with pytest.raises(ValueError, match=r'^OddsPath '):
        exact.decimal_of('x', what='OddsPath')
