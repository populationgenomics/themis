"""The price tables price every token type on each rate card, and each card follows the list's derivations."""

from __future__ import annotations

import decimal
import fractions

import pytest

from themis_infra import cost_metrics, cost_prices

# `cacheRead` is the input rate over this, per model; a tenth for a model not named here.
_CACHE_READ_DIVISOR = {'claude-sonnet-5-5': 20, 'claude-fable-5-1': 40, 'claude-fable-5-2': 40}
_DEFAULT_CACHE_READ_DIVISOR = 10
# `cacheCreation` at the 5-minute write rate is five quarters of the input rate.
_CACHE_CREATION_RATIO = fractions.Fraction(5, 4)


def _cards() -> list[tuple[str, str, cost_prices.RateCard]]:
    """Every rate card of both tables as (id, model, card)."""
    cards = [(model, model, card) for model, card in cost_prices.LIST_PRICES_CENTS_PER_MTOK.items()]
    for model, by_prompt_length in cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH.items():
        cards.extend((f'{model}-{prompt_length}', model, card) for prompt_length, card in by_prompt_length.items())
    return cards


_CARDS = [pytest.param(model, card, id=card_id) for card_id, model, card in _cards()]


@pytest.mark.parametrize(('model', 'card'), _CARDS)
def test_every_card_prices_exactly_the_four_token_types(model: str, card: cost_prices.RateCard) -> None:
    # A missing type would leave that share of a model's tokens unpriced with no alert to say so; a stray key would
    # never match a series.
    assert set(card) == set(cost_metrics.TOKEN_TYPES), model
    for token_type, cents in card.items():
        # Not a float, which holds few decimal prices exactly: the derivations below are checked exactly.
        assert isinstance(cents, int | decimal.Decimal), (model, token_type)
        assert cents > 0, (model, token_type)


@pytest.mark.parametrize(('model', 'card'), _CARDS)
def test_every_cards_cache_rates_derive_from_its_input_rate(model: str, card: cost_prices.RateCard) -> None:
    rates = {token_type: fractions.Fraction(cents) for token_type, cents in card.items()}
    divisor = _CACHE_READ_DIVISOR.get(model, _DEFAULT_CACHE_READ_DIVISOR)
    assert rates['cacheRead'] == rates['input'] / divisor, model
    assert rates['cacheCreation'] == rates['input'] * _CACHE_CREATION_RATIO, model
    assert rates['output'] > rates['input'], model


def test_the_committed_tables_price_each_model_once() -> None:
    # `priced_models` raises on a model in both tables; non-empty rules out a vacuous pass.
    assert cost_prices.priced_models()


def test_a_model_in_both_tables_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    card = {'input': 10, 'output': 50, 'cacheRead': 1, 'cacheCreation': decimal.Decimal('12.5')}
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK', {'a': card, 'b': card})
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH', {'b': {'upTo100k': card}})
    with pytest.raises(ValueError, match=r'both.*: b$'):
        cost_prices.priced_models()


@pytest.mark.parametrize('model', sorted(cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH))
def test_a_model_priced_by_prompt_length_has_a_card_for_every_length_the_counter_writes(model: str) -> None:
    # A length without a card would leave every request on that side of the boundary unpriced.
    cards = cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH[model]
    assert set(cards) == set(cost_metrics.PROMPT_LENGTHS), model
    # The longer prompt is the dearer one, on every token type.
    shorter, longer = (cards[prompt_length] for prompt_length in cost_metrics.PROMPT_LENGTHS)
    for token_type in cost_metrics.TOKEN_TYPES:
        assert longer[token_type] > shorter[token_type], (model, token_type)


def test_the_flat_rates_are_positive_cents() -> None:
    assert cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR > 0
    assert cost_prices.WEB_SEARCH_CENTS_PER_REQUEST > 0
