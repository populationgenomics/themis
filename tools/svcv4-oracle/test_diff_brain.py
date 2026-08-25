"""Network-free tests for the oracle's cap-diff brain (no Node subprocess, no fetch).

`oracle.py` lives in a hyphenated directory (not an importable package), so it is loaded by path.
"""

from __future__ import annotations

import decimal
import importlib.util
import pathlib

from themis.svcv4 import reference

_SPEC = importlib.util.spec_from_file_location('svcv4_oracle', pathlib.Path(__file__).parent / 'oracle.py')
assert _SPEC is not None
assert _SPEC.loader is not None
oracle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(oracle)

_NEG_INF = decimal.Decimal('-Infinity')
_ZERO = decimal.Decimal('0')


def _pin(ours: tuple[decimal.Decimal, decimal.Decimal], ref: tuple[decimal.Decimal, decimal.Decimal]) -> object:
    return oracle.Divergence(ours=ours, ref=ref, reason='pinned for test')


def test_matching_caps_pass() -> None:
    caps = {'CODE': (decimal.Decimal('-8'), decimal.Decimal('8'))}
    rows = oracle.compare_caps(caps, caps, {})
    assert [row.status for row in rows] == ['ok']


def test_expected_divergence_passes() -> None:
    ours = {'CODE': (decimal.Decimal('-4'), _ZERO)}
    ref = {'CODE': (_NEG_INF, _ZERO)}
    pins = {'CODE': _pin((decimal.Decimal('-4'), _ZERO), (_NEG_INF, _ZERO))}
    rows = oracle.compare_caps(ours, ref, pins)
    assert [row.status for row in rows] == ['EXPECTED']


def test_resolved_divergence_is_not_ok() -> None:
    """A pinned divergence whose sides have converged is RESOLVED (a failure), never a silent ok."""
    caps = {'CODE': (decimal.Decimal('-4'), _ZERO)}
    pins = {'CODE': _pin((decimal.Decimal('-1'), _ZERO), (_NEG_INF, _ZERO))}
    rows = oracle.compare_caps(caps, caps, pins)
    assert [row.status for row in rows] == ['RESOLVED']


def test_absent_code_pin_fails() -> None:
    """A pin naming a code absent from both tables is flagged DIFF, not silently dropped."""
    caps = {'REAL': (_ZERO, decimal.Decimal('1'))}
    pins = {'GHOST': _pin((_ZERO, _ZERO), (_ZERO, _ZERO))}
    rows = oracle.compare_caps(caps, caps, pins)
    ghost = [row for row in rows if row.key == 'GHOST']
    assert len(ghost) == 1
    assert ghost[0].status == 'DIFF'


def test_unpinned_mismatch_is_diff() -> None:
    ours = {'CODE': (decimal.Decimal('-4'), _ZERO)}
    ref = {'CODE': (decimal.Decimal('-8'), _ZERO)}
    rows = oracle.compare_caps(ours, ref, {})
    assert [row.status for row in rows] == ['DIFF']


def test_a_pin_for_a_cap_the_library_does_not_carry_passes() -> None:
    """A cap only the calculator states is pinned by its absence: there is no value on our side to pin."""
    ref = {'BUNDLE_ONLY': (_NEG_INF, decimal.Decimal('Infinity'))}
    pins = {'BUNDLE_ONLY': oracle.Divergence(ours=None, ref=ref['BUNDLE_ONLY'], reason='pinned for test')}
    rows = oracle.compare_caps({}, ref, pins)
    assert [row.status for row in rows] == ['EXPECTED']


def test_an_unpinned_one_sided_cap_is_diff() -> None:
    ref = {'BUNDLE_ONLY': (_NEG_INF, decimal.Decimal('Infinity'))}
    rows = oracle.compare_caps({}, ref, {})
    assert [row.status for row in rows] == ['DIFF']


def test_pins_state_what_the_library_currently_carries() -> None:
    """A pin's `ours` side that has drifted from the library describes a divergence that is no longer ours.

    Reaching the library needs no Node and no network, so this catches a stale pin where CI runs rather
    than on the next by-hand oracle run.
    """
    ref_lib = reference.load_reference()
    tables = {
        'evidence code': (
            oracle.EXPECTED_CODE_DIVERGENCES,
            {name: (spec.low, spec.high) for name, spec in ref_lib.codes.items()},
        ),
        'concept cap': (
            oracle.EXPECTED_CONCEPT_CAP_DIVERGENCES,
            {name: (cap.low, cap.high) for name, cap in ref_lib.concept_caps.items()},
        ),
        'category cap': (
            oracle.EXPECTED_CATEGORY_CAP_DIVERGENCES,
            {name: (cap.low, cap.high) for name, cap in ref_lib.category_caps.items()},
        ),
    }
    assert any(pins for pins, _ in tables.values())  # non-vacuous: something is pinned
    for kind, (pins, carried) in tables.items():
        for key, pin in pins.items():
            assert carried.get(key) == pin.ours, f'{kind} {key} pin no longer states what the library carries'
