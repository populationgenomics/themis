"""Diff the themis.svcv4 combining engine against the ClinGen SVCv4 pilot calculator.

Proves our combining engine still matches the reference. Drives `reference_oracle.js`, which fetches
the calculator's `calc-phase3.js` at runtime and answers probe questions with its own cap tables and
combining functions, then compares against `themis.svcv4`:

  - the cap tables (`EVIDENCE_CODE_CAP` / `_CONCEPT_CAP` / `_CATEGORY_CAP` / `_CONCEPT_TO_CODES`)
    against the loaded reference's per-code ranges, concept caps, category caps, and concept->codes;
  - `calClassification` banding against `scoring.band_for_total` across the classification edges;
  - `getMaxOrMin` (missense-vs-splice max path) against `scoring.select_path`;
  - `applyConstraint` clamping against `scoring.clamp`.

Three pin maps — `EXPECTED_CODE_DIVERGENCES`, `EXPECTED_CONCEPT_CAP_DIVERGENCES` and
`EXPECTED_CATEGORY_CAP_DIVERGENCES` — carry the caps that diverge, each reported as expected rather
than failing the run. The library takes its caps from the supplements, so a cap the calculator
states and no supplement does is a divergence by construction: the README lists them and each pin
gives its ground. A change to either side of a pin, or any other mismatch, is an unexpected diff.

Run on demand (not in CI): `uv run python tools/svcv4-oracle/oracle.py`. Needs Node 24+ and network.
Exits non-zero on any unexpected diff, so it doubles as a manual gate. See the README for what the
bundle does not carry (the per-code point-value tables, which are out of scope here).
"""

from __future__ import annotations

import decimal
import json
import pathlib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import NamedTuple

# themis is a repo-root namespace package and the repo is not pip-installed; put the root on the path
# so this runs standalone (`uv run python tools/svcv4-oracle/oracle.py`), the hyphenated tool
# directory ruling out the repo's usual `python -m tools.<pkg>` invocation.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from themis.svcv4 import reference, scoring

_HERE = pathlib.Path(__file__).resolve().parent
_ORACLE_JS = _HERE / 'reference_oracle.js'

_NEG_INF = decimal.Decimal('-Infinity')
_POS_INF = decimal.Decimal('Infinity')

# Probe inputs, owned here so both engines score identical inputs (the two lists cannot drift).
BAND_EDGE_POINTS: tuple[str, ...] = (
    '-5',
    '-4',
    '-3.999',
    '-1.001',
    '-1',
    '-0.999',
    '0',
    '1.999',
    '2',
    '3.999',
    '4',
    '5.999',
    '6',
    '9.999',
    '10',
    '11',
)
MAX_PATH_PAIRS: tuple[tuple[int, int], ...] = ((-3, 0), (-3, -1), (0, 0), (2, 2), (-1, 0), (4, 3))
# (low, high, value); a None bound is the unbounded (NA) side of the cap.
CLAMP_PROBES: tuple[tuple[int | None, int | None, int], ...] = (
    (-8, 8, 10),
    (-8, 8, -10),
    (-8, 8, 3),
    (None, 0, -20),
    (None, 0, 5),
    (0, None, -5),
    (0, None, 7),
    (-4, 4, -6),
)

# Band code + VUS sub-band -> the calculator's calClassification label.
_BAND_LABEL: dict[tuple[str, str | None], str] = {
    ('B', None): 'Benign',
    ('LB', None): 'Likely Benign',
    ('VUS', 'VUS-low'): 'VUS Low',
    ('VUS', 'VUS-mid'): 'VUS Mid',
    ('VUS', 'VUS-high'): 'VUS High',
    ('LP', None): 'Likely Pathogenic',
    ('P', None): 'Pathogenic',
}


class OracleError(Exception):
    """The reference-oracle subprocess failed or returned an unusable response."""


class Divergence(NamedTuple):
    """A pinned expected difference between what the library carries for one key and the calculator's cap.

    `ours` is None where the library deliberately carries no such cap: the calculator holds one the
    supplements never state, so there is no value to pin, only the absence.
    """

    ours: tuple[decimal.Decimal, decimal.Decimal] | None
    ref: tuple[decimal.Decimal, decimal.Decimal]
    reason: str


EXPECTED_CODE_DIVERGENCES: dict[str, Divergence] = {
    'CDS_PRD': Divergence(
        ours=(decimal.Decimal('-1'), decimal.Decimal('6')),
        ref=(decimal.Decimal('-4'), decimal.Decimal('6')),
        reason='SM8 para 32 and SM10 para 32 state the range as -1.0 to +6.0; the calculator floors it at -4.0',
    ),
    'CLN_DNV': Divergence(
        ours=(decimal.Decimal('0'), _POS_INF),
        ref=(decimal.Decimal('0'), decimal.Decimal('12')),
        reason='SM4 sums de novo points across probands under no cap; +7.0 is per proband, so we leave it unbounded',
    ),
    'LOC_SEG': Divergence(
        ours=(decimal.Decimal('-4'), decimal.Decimal('4')),
        ref=(decimal.Decimal('0'), decimal.Decimal('4')),
        reason='SM5 states both -4.0 (para 33) and a 0.0 floor (title, para 34); we follow -4.0',
    ),
    'NUL_PRD': Divergence(
        ours=(decimal.Decimal('0'), decimal.Decimal('6')),
        ref=(decimal.Decimal('-4'), decimal.Decimal('6')),
        reason="every NUL_PRD path floors at 0.0; the whole-gene +10.0 is the NUL_PFD category cap's, not this code's",
    ),
    'POP_HMZ': Divergence(
        ours=(_NEG_INF, decimal.Decimal('0')),
        ref=(decimal.Decimal('-4'), decimal.Decimal('0')),
        reason='SM3 states per-occurrence tariffs (para 72) and no code-level bound, so the benign side accumulates',
    ),
}

EXPECTED_CONCEPT_CAP_DIVERGENCES: dict[str, Divergence] = {
    'POP': Divergence(
        ours=(_NEG_INF, decimal.Decimal('0')),
        ref=(decimal.Decimal('-10'), decimal.Decimal('0')),
        reason='no supplement floors the POP sum; SM3 makes both POP codes benign-only, which is the 0.0 ceiling',
    ),
    'SPL_PRD_SPA': Divergence(
        ours=(decimal.Decimal('-3'), decimal.Decimal('6')),
        ref=(decimal.Decimal('-8'), decimal.Decimal('6')),
        reason='the supplements cap this combine once per colour, not once; we carry the union, floored at -3.0',
    ),
}

EXPECTED_CATEGORY_CAP_DIVERGENCES: dict[str, Divergence] = {
    'HOD': Divergence(
        ours=None,
        ref=(_NEG_INF, _POS_INF),
        reason='no supplement names an HOD category; the library carries no cap for one',
    ),
}


class BandRow(NamedTuple):
    pt: str
    label: str


class MaxPathRow(NamedTuple):
    mis: int
    spl: int
    key: str
    value: decimal.Decimal


class ClampRow(NamedTuple):
    low: int | None
    high: int | None
    value: int
    result: decimal.Decimal


class Response(NamedTuple):
    """The reference oracle's answers, with cap bounds normalised to decimals (NA -> +/-Infinity)."""

    source_url: str
    fetched_bytes: int
    code_caps: dict[str, tuple[decimal.Decimal, decimal.Decimal]]
    concept_caps: dict[str, tuple[decimal.Decimal, decimal.Decimal]]
    category_caps: dict[str, tuple[decimal.Decimal, decimal.Decimal]]
    concept_to_codes: dict[str, tuple[str, ...]]
    banding: tuple[BandRow, ...]
    max_path: tuple[MaxPathRow, ...]
    clamp: tuple[ClampRow, ...]


class Row(NamedTuple):
    status: str  # 'ok' | 'EXPECTED' | 'RESOLVED' | 'DIFF'
    key: str
    ours: str
    ref: str
    note: str = ''


def _norm_bound(value: object, na_sentinel: str, *, is_low: bool) -> decimal.Decimal:
    if value == na_sentinel:
        return _NEG_INF if is_low else _POS_INF
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OracleError(f'unexpected cap bound {value!r}')
    return decimal.Decimal(str(value))


def _norm_cap(pair: Sequence[object], na_sentinel: str) -> tuple[decimal.Decimal, decimal.Decimal]:
    if len(pair) != 2:
        raise OracleError(f'cap entry {pair!r} is not a [low, high] pair')
    return (_norm_bound(pair[0], na_sentinel, is_low=True), _norm_bound(pair[1], na_sentinel, is_low=False))


def _num(value: object, context: str) -> decimal.Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise OracleError(f'{context}: expected a number, got {value!r}')
    try:
        return decimal.Decimal(str(value))
    except decimal.InvalidOperation as e:
        raise OracleError(f'{context}: {value!r} is not numeric') from e


def _parse_response(raw: str) -> Response:
    try:
        data = json.loads(raw)
        na_sentinel = data['na_sentinel']
        if not isinstance(na_sentinel, str):
            raise OracleError(f'na_sentinel must be a string, got {na_sentinel!r}')
        caps = data['caps']
        return Response(
            source_url=str(data['source_url']),
            fetched_bytes=int(data['fetched_bytes']),
            code_caps={k: _norm_cap(v, na_sentinel) for k, v in caps['code'].items()},
            concept_caps={k: _norm_cap(v, na_sentinel) for k, v in caps['concept'].items()},
            category_caps={k: _norm_cap(v, na_sentinel) for k, v in caps['category'].items()},
            concept_to_codes={k: tuple(str(c) for c in v) for k, v in caps['concept_to_codes'].items()},
            banding=tuple(BandRow(pt=str(r['pt']), label=str(r['label'])) for r in data['banding']),
            max_path=tuple(
                MaxPathRow(
                    mis=int(r['mis']),
                    spl=int(r['spl']),
                    key=str(r['key']),
                    value=_num(r['value'], 'max_path.value'),
                )
                for r in data['max_path']
            ),
            clamp=tuple(
                ClampRow(low=r['low'], high=r['high'], value=r['value'], result=_num(r['result'], 'clamp.result'))
                for r in data['clamp']
            ),
        )
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as e:
        # AttributeError catches a future list-typed cap table (`.items()` on a list) as a clean
        # OracleError rather than a bare AttributeError.
        raise OracleError(f'malformed reference-oracle response: {e}') from e


def run_reference_oracle() -> Response:
    """Run the Node reference oracle over the probe inputs and parse its response.

    Raises:
        OracleError: If Node is absent, the oracle exits non-zero, or its output is unparseable.
    """
    request = json.dumps(
        {
            'banding_points': list(BAND_EDGE_POINTS),
            'max_path_pairs': [list(pair) for pair in MAX_PATH_PAIRS],
            'clamp_probes': [list(probe) for probe in CLAMP_PROBES],
        }
    )
    try:
        # Literal argv, no untrusted input; the oracle reads its probe request from stdin.
        # --permission (Node 24 Permission Model) grants read only to the oracle script and no
        # fs-write / child_process, so even a vm escape from the untrusted bundle cannot write the
        # fetched source to disk or spawn a shell; network (fetch) is not gated by the model.
        proc = subprocess.run(  # noqa: S603
            ['node', '--permission', f'--allow-fs-read={_ORACLE_JS}', str(_ORACLE_JS)],  # noqa: S607
            input=request,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise OracleError('node not found on PATH; the reference oracle needs Node 24+ (--permission model)') from e
    except subprocess.CalledProcessError as e:
        raise OracleError(f'reference oracle failed (exit {e.returncode}):\n{e.stderr.strip()}') from e
    return _parse_response(proc.stdout)


def _fmt(d: decimal.Decimal) -> str:
    if d == _NEG_INF:
        return '-inf'
    if d == _POS_INF:
        return '+inf'
    text = format(d, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _fmt_pair(pair: tuple[decimal.Decimal, decimal.Decimal] | None) -> str:
    return '(absent)' if pair is None else f'[{_fmt(pair[0])}, {_fmt(pair[1])}]'


def compare_caps(
    ours: Mapping[str, tuple[decimal.Decimal, decimal.Decimal]],
    ref: Mapping[str, tuple[decimal.Decimal, decimal.Decimal]],
    divergences: Mapping[str, Divergence],
) -> list[Row]:
    """Diff two cap tables, honouring pinned expected divergences.

    A pinned divergence whose two sides now match (`ours == ref`) is reported as RESOLVED (an
    unexpected status: the pin should be removed), never silently passed. A pin referencing a key
    absent from both tables is flagged too. A key present on one side only is a DIFF unless a pin
    states that absence. Any unpinned mismatch is a DIFF.
    """
    rows: list[Row] = []
    for key in sorted(set(ours) | set(ref)):
        o = ours.get(key)
        r = ref.get(key)
        div = divergences.get(key)
        if o is not None and o == r:
            if div is not None:
                rows.append(Row('RESOLVED', key, _fmt_pair(o), _fmt_pair(r), 'divergence resolved — remove the pin'))
            else:
                rows.append(Row('ok', key, _fmt_pair(o), _fmt_pair(r)))
        elif div is not None and o == div.ours and r == div.ref:
            rows.append(Row('EXPECTED', key, _fmt_pair(o), _fmt_pair(r), div.reason))
        elif o is None or r is None:
            rows.append(Row('DIFF', key, _fmt_pair(o), _fmt_pair(r), 'present on one side only'))
        else:
            rows.append(Row('DIFF', key, _fmt_pair(o), _fmt_pair(r), 'unexpected'))
    present = set(ours) | set(ref)
    for key in sorted(divergences):
        if key not in present:
            rows.append(Row('DIFF', key, '(absent)', '(absent)', 'pin references a code absent from both tables'))
    return rows


def compare_concept_to_codes(ours: Mapping[str, tuple[str, ...]], ref: Mapping[str, tuple[str, ...]]) -> list[Row]:
    """Diff the concept-to-codes mapping (which codes each concept sums); code order is irrelevant."""
    rows: list[Row] = []
    for key in sorted(set(ours) | set(ref)):
        o = ours.get(key)
        r = ref.get(key)
        match = o is not None and r is not None and set(o) == set(r)
        rows.append(
            Row(
                'ok' if match else 'DIFF',
                key,
                '(absent)' if o is None else ','.join(o),
                '(absent)' if r is None else ','.join(r),
            )
        )
    return rows


def compare_banding(ref_lib: reference.Reference, probes: Sequence[str], banding: Sequence[BandRow]) -> list[Row]:
    """Diff scoring.band_for_total against the calculator's calClassification across band edges.

    Scores the Python-owned probe (not the value Node echoes) and asserts the echo matches, so a
    truncated or echo-drifted response fails loud instead of vacuously passing.

    Raises:
        OracleError: If an echoed probe does not equal the Python probe it is paired with.
    """
    rows: list[Row] = []
    for probe, br in zip(probes, banding, strict=True):
        if br.pt != probe:
            raise OracleError(f'banding echo drift: sent {probe!r}, oracle returned {br.pt!r}')
        band, sub = scoring.band_for_total(ref_lib, decimal.Decimal(probe))
        ours_label = _BAND_LABEL.get((band, sub))
        if ours_label is None:
            rows.append(Row('DIFF', f'pt={probe}', f'({band},{sub})', br.label, 'our band/sub not in label map'))
        else:
            rows.append(Row('ok' if ours_label == br.label else 'DIFF', f'pt={probe}', ours_label, br.label))
    return rows


def _mk(total: int) -> scoring.PathResult:
    d = decimal.Decimal(total)
    return scoring.PathResult(
        label='x', parent_code='x', raw_prd=d, adjusted_prd=d, multiplier=decimal.Decimal(1), total=d, contributions=()
    )


def compare_max_path(pairs: Sequence[tuple[int, int]], max_path: Sequence[MaxPathRow]) -> list[Row]:
    """Diff scoring.select_path against the calculator's getMaxOrMin over the Python-owned probe pairs.

    Raises:
        OracleError: If an echoed (MIS, SPL) pair does not equal the Python probe it is paired with.
    """
    rows: list[Row] = []
    for (mis, spl), mr in zip(pairs, max_path, strict=True):
        if (mr.mis, mr.spl) != (mis, spl):
            raise OracleError(f'max-path echo drift: sent {(mis, spl)}, oracle returned {(mr.mis, mr.spl)}')
        amino = _mk(mis)
        splice = _mk(spl)
        selected, _ = scoring.select_path(amino, splice)
        ours_key = 'MIS' if selected is amino else 'SPL'
        match = ours_key == mr.key and selected.total == mr.value
        rows.append(
            Row(
                'ok' if match else 'DIFF',
                f'MIS={mis},SPL={spl}',
                f'{ours_key}={_fmt(selected.total)}',
                f'{mr.key}={_fmt(mr.value)}',
            )
        )
    return rows


def compare_clamp(probes: Sequence[tuple[int | None, int | None, int]], clamp: Sequence[ClampRow]) -> list[Row]:
    """Diff scoring.clamp against the calculator's applyConstraint over the Python-owned probe caps.

    Raises:
        OracleError: If an echoed (low, high, value) probe does not equal the Python probe it pairs with.
    """
    rows: list[Row] = []
    for (low_p, high_p, value_p), cr in zip(probes, clamp, strict=True):
        if (cr.low, cr.high, cr.value) != (low_p, high_p, value_p):
            raise OracleError(
                f'clamp echo drift: sent {(low_p, high_p, value_p)}, oracle returned {(cr.low, cr.high, cr.value)}'
            )
        low = _NEG_INF if low_p is None else decimal.Decimal(low_p)
        high = _POS_INF if high_p is None else decimal.Decimal(high_p)
        ours = scoring.clamp(decimal.Decimal(value_p), low, high)
        rows.append(
            Row(
                'ok' if ours == cr.result else 'DIFF',
                f'clamp([{_fmt(low)}, {_fmt(high)}], {value_p})',
                _fmt(ours),
                _fmt(cr.result),
            )
        )
    return rows


def _ours_code_caps(ref_lib: reference.Reference) -> dict[str, tuple[decimal.Decimal, decimal.Decimal]]:
    return {code: (spec.low, spec.high) for code, spec in ref_lib.codes.items()}


def _ours_cap_map(caps: Mapping[str, reference.CapRange]) -> dict[str, tuple[decimal.Decimal, decimal.Decimal]]:
    return {name: (cap.low, cap.high) for name, cap in caps.items()}


def _format_row(row: Row) -> str:
    tag = {'ok': 'ok      ', 'EXPECTED': 'EXPECTED', 'RESOLVED': 'RESOLVED', 'DIFF': 'DIFF    '}[row.status]
    line = f'  {tag}  {row.key:<24} ours={row.ours:<18} ref={row.ref}'
    return f'{line}   ({row.note})' if row.note else line


def main() -> int:
    ref_lib = reference.load_reference()
    response = run_reference_oracle()

    sections: list[tuple[str, list[Row]]] = [
        (
            '[1] Evidence-code caps  (evidence_codes range vs EVIDENCE_CODE_CAP)',
            compare_caps(_ours_code_caps(ref_lib), response.code_caps, EXPECTED_CODE_DIVERGENCES),
        ),
        (
            '[2] Concept caps  (concept_caps vs EVIDENCE_CONCEPT_CAP)',
            compare_caps(_ours_cap_map(ref_lib.concept_caps), response.concept_caps, EXPECTED_CONCEPT_CAP_DIVERGENCES),
        ),
        (
            '[3] Category caps  (category_caps vs EVIDENCE_CATEGORY_CAP)',
            compare_caps(
                _ours_cap_map(ref_lib.category_caps), response.category_caps, EXPECTED_CATEGORY_CAP_DIVERGENCES
            ),
        ),
        (
            '[4] Concept->codes  (concept_to_codes vs EVIDENCE_CONCEPT_TO_CODES)',
            compare_concept_to_codes(ref_lib.concept_to_codes, response.concept_to_codes),
        ),
        (
            '[5] Banding  (band_for_total vs calClassification)',
            compare_banding(ref_lib, BAND_EDGE_POINTS, response.banding),
        ),
        ('[6] Max-path  (select_path vs getMaxOrMin)', compare_max_path(MAX_PATH_PAIRS, response.max_path)),
        ('[7] Clamp  (scoring.clamp vs applyConstraint)', compare_clamp(CLAMP_PROBES, response.clamp)),
    ]

    print('SVCv4 reference oracle — themis.svcv4  vs  ClinGen pilot calc-phase3.js')
    print(f'fetched {response.fetched_bytes} bytes from {response.source_url}\n')

    unexpected = 0
    expected = 0
    for title, rows in sections:
        print(title)
        for row in rows:
            if row.status in ('DIFF', 'RESOLVED'):
                unexpected += 1
            elif row.status == 'EXPECTED':
                expected += 1
            print(_format_row(row))
        print()

    verdict = 'PASS' if unexpected == 0 else 'FAIL'
    print(f'{unexpected} unexpected finding(s) (DIFF/RESOLVED), {expected} expected divergence(s).  {verdict}')
    return 1 if unexpected else 0


if __name__ == '__main__':
    raise SystemExit(main())
