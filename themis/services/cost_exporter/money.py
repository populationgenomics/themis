"""Money as the report shows it: the metrics hold cents, the reader sees dollars, the sign leads (`-$0.50`)."""

from __future__ import annotations

_CENTS_PER_DOLLAR = 100


def to_dollars(cents: float) -> float:
    """A cents figure in dollars, for plotting."""
    return cents / _CENTS_PER_DOLLAR


def format_dollars(amount: float) -> str:
    """A dollar amount for a reader: `$12.34`, `-$0.50`; an amount that rounds to zero carries no sign."""
    amount = round(amount, 2)
    sign = '-' if amount < 0 else ''
    return f'{sign}${abs(amount):,.2f}'


def dollars(cents: float) -> str:
    """A cents figure for a reader, in dollars."""
    return format_dollars(to_dollars(cents))
