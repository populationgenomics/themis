"""A figure as the exact decimal it was quoted as, whatever type the caller holds it in.

Every threshold this library compares against is a decimal the framework publishes — a DAFT of 0.000118, a
Tavtigian bound of 18.7, an AlphaMissense bin edge of 0.791 — and the figure compared to it arrives from an
upstream as a JSON number, which Python hands over as a float. `decimal.Decimal(0.791)` is
0.791000000000000036…, the float's binary expansion, which compares above the bound the figure sits on: a value on
a bin edge lands in the neighbouring bin with nothing raising. The float's shortest round-trip repr — `str` — is
the decimal it was parsed from, and the form the bounds are written in.

So every entry point that takes a figure from outside accepts it as the caller holds it — a `Decimal`, a float, an
int, or a decimal string — and converts here, once. A `Decimal` the caller already built from a float arrives
carrying the expansion, the one form in which the error is silent, and fails the digit bound: a float
carries at most 17 significant digits, and no published figure needs that many.
"""

from __future__ import annotations

import decimal

Figure = decimal.Decimal | float | int | str
"""A figure as a caller holds it: an upstream's JSON number, or an exact decimal string or `Decimal`."""

_MAX_SIGNIFICANT_DIGITS = 17


def decimal_of(value: Figure, *, what: str) -> decimal.Decimal:
    """`value` as an exact decimal, a float read through its shortest round-trip repr.

    Args:
        value: The figure, as the caller holds it.
        what: What the figure is — "predictor score", "FAF" — for the error message.

    Raises:
        ValueError: If `value` is a bool or not a finite number, or carries more significant digits
            than a quoted figure can.
    """
    if isinstance(value, bool):
        raise ValueError(f'{what} is not a number: {value!r}')
    if isinstance(value, decimal.Decimal):
        exact = value
    else:
        try:
            exact = decimal.Decimal(str(value))
        except decimal.InvalidOperation as e:
            raise ValueError(f'{what} is not a number: {value!r}') from e
    if not exact.is_finite():
        raise ValueError(f'{what} must be finite, got {value!r}')
    digits = len(exact.as_tuple().digits)
    if digits > _MAX_SIGNIFICANT_DIGITS:
        remedy = (
            'A Decimal built from a float reads this way, and comparing it puts a figure on a bound one step off; '
            'pass the float itself.'
            if isinstance(value, decimal.Decimal)
            else 'No published figure is quoted to that many.'
        )
        raise ValueError(
            f'{what} carries {digits} significant digits, over the {_MAX_SIGNIFICANT_DIGITS} a quoted figure can: '
            f'{value!r}. {remedy}'
        )
    return exact
