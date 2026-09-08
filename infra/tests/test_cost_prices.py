"""The price table prices every token type of every model it names, and each row follows the list's derivations."""

from __future__ import annotations

import pytest

from themis_infra import cost_metrics, cost_prices

# `cacheRead` is the input rate over this, per model; Fable 5.1 is the one exception to a tenth.
_CACHE_READ_DIVISOR = {'claude-fable-5-1': 40}
_DEFAULT_CACHE_READ_DIVISOR = 10
# `cacheCreation` at the 5-minute write rate is five quarters of the input rate.
_CACHE_CREATION_NUMERATOR, _CACHE_CREATION_DENOMINATOR = 5, 4


@pytest.mark.parametrize('model', sorted(cost_prices.LIST_PRICES_CENTS_PER_MTOK))
def test_every_row_prices_exactly_the_four_token_types(model: str) -> None:
    # A missing type would leave that share of a model's tokens unpriced with no alert to say so; a stray key would
    # never match a series.
    row = cost_prices.LIST_PRICES_CENTS_PER_MTOK[model]
    assert set(row) == set(cost_metrics.TOKEN_TYPES), model
    for token_type, cents in row.items():
        assert isinstance(cents, int), (model, token_type)
        assert cents > 0, (model, token_type)


@pytest.mark.parametrize('model', sorted(cost_prices.LIST_PRICES_CENTS_PER_MTOK))
def test_every_rows_cache_rates_derive_from_its_input_rate(model: str) -> None:
    row = cost_prices.LIST_PRICES_CENTS_PER_MTOK[model]
    divisor = _CACHE_READ_DIVISOR.get(model, _DEFAULT_CACHE_READ_DIVISOR)
    assert row['input'] % divisor == 0, model
    assert row['cacheRead'] == row['input'] // divisor, model
    assert row['input'] * _CACHE_CREATION_NUMERATOR % _CACHE_CREATION_DENOMINATOR == 0, model
    assert row['cacheCreation'] == row['input'] * _CACHE_CREATION_NUMERATOR // _CACHE_CREATION_DENOMINATOR, model
    assert row['output'] > row['input'], model


def test_the_flat_rates_are_positive_cents() -> None:
    assert cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR > 0
    assert cost_prices.WEB_SEARCH_CENTS_PER_REQUEST > 0
