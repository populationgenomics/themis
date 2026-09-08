"""Cents read as dollars with the sign ahead of the currency, the one way everywhere the report shows money."""

from __future__ import annotations

import pytest

from themis.services.cost_exporter import money


@pytest.mark.parametrize(
    ('cents', 'text'),
    [
        pytest.param(1234, '$12.34', id='cents-to-dollars'),
        pytest.param(123456.7, '$1,234.57', id='dollars-with-thousands'),
        pytest.param(-50, '-$0.50', id='negative-cents'),
        pytest.param(0, '$0.00', id='zero-cents'),
        pytest.param(-0.001, '$0.00', id='negative-below-a-cent-is-not-signed'),
    ],
)
def test_cents_display_as_dollars(cents: float, text: str) -> None:
    assert money.dollars(cents) == text


def test_the_chart_axis_and_the_message_place_the_sign_the_same_way() -> None:
    assert money.format_dollars(-3.0) == money.dollars(-300) == '-$3.00'
    assert money.format_dollars(money.to_dollars(1234)) == money.dollars(1234)
