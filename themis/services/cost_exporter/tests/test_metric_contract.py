"""The vocabulary the producers write under is the one the infrastructure's queries read — and nothing else.

The Pulumi program runs in its own interpreter and imports nothing from `themis`, and the producers
import nothing from the program, so each names the metrics, the labels and the token types on its own;
this holds the two together both ways, the way `tests/test_images_manifest.py` holds the image
overrides to the program. The program's module is loaded by path, not through its package, whose
`__init__` imports the Pulumi SDK.
"""

from __future__ import annotations

import importlib.util
import pathlib

from themis.telemetry import names

_COST_METRICS = pathlib.Path(__file__).resolve().parents[4] / 'infra' / 'themis_infra' / 'cost_metrics.py'

# Producer-side constants that are not vocabulary: the meter's scope name is the producers' own.
_NOT_VOCABULARY = frozenset({'METER_NAME'})


def _infra_vocabulary() -> dict[str, object]:
    spec = importlib.util.spec_from_file_location('cost_metrics', _COST_METRICS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: value for name, value in vars(module).items() if name.isupper()}


def _producer_constants() -> dict[str, str]:
    return {name: value for name, value in vars(names).items() if name.isupper() and isinstance(value, str)}


def test_the_infrastructure_names_exactly_what_the_producers_write() -> None:
    constants = _producer_constants()
    vocabulary: dict[str, object] = {name: value for name, value in constants.items() if name not in _NOT_VOCABULARY}
    vocabulary['TOKEN_TYPES'] = tuple(token_type.value for token_type in names.TokenType)
    assert len(vocabulary) > 1  # a vocabulary of nothing would hold trivially

    assert _infra_vocabulary() == vocabulary


def test_every_producer_constant_is_either_vocabulary_or_carved_out() -> None:
    # A constant neither bound nor named in the carve-out is one the program's copy silently lacks.
    assert set(_producer_constants()) - set(_infra_vocabulary()) == _NOT_VOCABULARY
