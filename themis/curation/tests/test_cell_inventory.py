"""Every cell a curator can answer is one the framework can account for.

`apps/web/curation-cells.json` is generated from the transcribed workflows (`bun run cells`), because
only that side knows which cells exist -- ids are built programmatically, so a phenotype row crossed
with five zygosity columns yields twenty-five that appear nowhere as literals. This is the join: the
worksheet says what it can emit, and the library says what it can account for.

The split is derived from the framework, never listed. A hand-kept exclusion list is one anybody can
grow to silence a failure instead of mapping a row, which turns the check into a formality.
"""

from __future__ import annotations

import decimal
import json
import pathlib
from typing import NotRequired, TypedDict

import pytest

from themis.svcv4 import data, observations, reference

_INVENTORY = pathlib.Path(__file__).resolve().parents[3] / 'apps' / 'web' / 'curation-cells.json'


class InventoryCell(TypedDict):
    """One row of `curation-cells.json`, as `apps/web/scripts/cells.ts` writes it.

    The two optional members are carried only by a cell the framework defines with a ratio -- POP_FRQ's
    four frequency rows -- and are what the worksheet derives that row with.
    """

    workflow: str
    code: str
    cell: str
    label: str
    min_multiple: NotRequired[float]
    bars_rarity_gated_codes: NotRequired[bool]


@pytest.fixture(scope='module')
def ref() -> reference.Reference:
    return data.load_reference()


@pytest.fixture(scope='module')
def cells() -> list[InventoryCell]:
    if not _INVENTORY.exists():
        raise AssertionError(f'{_INVENTORY} is missing; regenerate it with `bun run cells` in apps/web')
    payload = json.loads(_INVENTORY.read_text('utf-8'))
    entries: list[InventoryCell] = payload['cells']
    # Non-empty rules out a vacuous pass if the generator ever stops walking the registry.
    assert entries, 'the inventory lists no cells'
    return entries


def test_every_independent_code_cell_is_priced(ref: reference.Reference, cells: list[InventoryCell]) -> None:
    """A cell on an independent code must resolve, or a curator's observation cannot be scored."""
    priced = observations.cell_points(ref)
    unvalued = observations.unvalued_cells(ref)
    unpriced = [
        entry['cell']
        for entry in cells
        if entry['code'] in ref.codes and not observations.is_path_code(ref, entry['code'])
        if entry['cell'] not in priced and entry['cell'] not in unvalued
    ]
    assert not unpriced, (
        f'{len(unpriced)} cell(s) on independent codes have no per-observation price: '
        f'{sorted(unpriced)[:8]}. Map them in themis/svcv4/observations.py, or -- where the '
        'framework states no value -- carry the row with its points None, which unvalued_cells reports.'
    )


def test_no_path_code_cell_is_priced(ref: reference.Reference, cells: list[InventoryCell]) -> None:
    """A variant-type cell must NOT resolve here.

    Those codes are priced by the decision-tree path a `builders` call selects, from a tier plus the
    matrix axes. One acquiring a per-observation price would be counted twice, or counted flat where
    the matrix should have scaled it -- a wrong total that still looks complete.
    """
    priced = observations.cell_points(ref)
    leaked = [
        entry['cell']
        for entry in cells
        if entry['code'] in ref.codes and observations.is_path_code(ref, entry['code'])
        if entry['cell'] in priced
    ]
    assert not leaked, f'variant-type cells must not carry a lookup price: {sorted(leaked)[:8]}'


def test_a_cell_naming_no_evidence_code_is_an_input_not_a_score(
    ref: reference.Reference, cells: list[InventoryCell]
) -> None:
    """A cell whose code is not one evidence code must not price, and must still name real ones.

    Two shapes reach here legitimately. The mechanism/exon matrix feeds `scoring.matrix_multiplier`
    rather than scoring anything. And several calculator tables print `Evidence Code:` blank because
    the family follows the path the curator takes -- a start-loss table is `NUL_` on its no-rescue
    row and `CDS_` on the rest -- so the transcription names both, joined. Either way the cell is not
    a per-observation row and must not carry a lookup price; what is checked is that the parts are
    codes the framework defines, so a typo cannot hide behind the exemption.
    """
    priced = observations.cell_points(ref)
    for entry in cells:
        code = entry['code']
        if code in ref.codes:
            continue
        for part in code.split('/'):
            assert part == 'SM18' or part in ref.codes, (
                f'{entry["cell"]} names {part!r}, which the framework does not define'
            )
        assert entry['cell'] not in priced, (
            f'{entry["cell"]} is priced by a path or a matrix axis and must not carry a row price'
        )


def test_the_frequency_rows_derive_from_the_library_s_own_bins(
    ref: reference.Reference, cells: list[InventoryCell]
) -> None:
    """The worksheet derives POP_FRQ's row; these are the numbers it derives it with.

    The worksheet selects the row from the FAF/DAFT multiple rather than asking a curator to eyeball
    it, which puts a second statement of `frequency.pop_frq`'s thresholds on the other side of the
    wire. This is what stops the two drifting: the rows carrying a multiple must be exactly the
    library's bins, in the library's order, at the library's values. A framework revision to the bins
    fails here instead of silently binning a variant one row out.
    """
    binned = [entry for entry in cells if 'min_multiple' in entry]
    assert [decimal.Decimal(str(entry['min_multiple'])) for entry in binned] == [
        frequency_bin.min_multiple for frequency_bin in ref.frequency_bins
    ], (
        'the transcribed frequency rows do not state the reference bins: regenerate the inventory '
        '(`bun run cells`), and reconcile apps/web/src/curation/workflows/frequency.ts with '
        'FREQUENCY_BINS in themis/svcv4/data/population.py'
    )


def test_the_barring_rows_are_the_ones_the_reference_prices_below_minus_one(
    ref: reference.Reference, cells: list[InventoryCell]
) -> None:
    """The rarity gate, stated once in points and once in rows, has to mean the same thing.

    The calculator words the gate as "applicable if Frequency >= -1.0"; the worksheet holds no points,
    so it flags the rows instead. Deriving the expected set from the reference's own points is what
    makes the flags checkable rather than asserted.
    """
    barring = {entry['cell'] for entry in cells if entry.get('bars_rarity_gated_codes')}
    expected = {
        entry['cell']
        for entry, frequency_bin in zip([e for e in cells if 'min_multiple' in e], ref.frequency_bins, strict=True)
        if frequency_bin.points < decimal.Decimal('-1.0')
    }
    # Non-empty both ways: a gate that barred nothing, or everything, would pass a set comparison
    # alone while meaning something the framework does not.
    assert expected, 'no reference bin scores below -1.0; the gate would bar nothing'
    assert barring == expected


def test_the_inventory_matches_the_pinned_transcription() -> None:
    """The inventory states the version it was generated from, since a worksheet pins one.

    A stale inventory would check the wrong cell set against the right library.
    """
    payload = json.loads(_INVENTORY.read_text('utf-8'))
    version = pathlib.Path(_INVENTORY.parent / 'src' / 'curation' / 'version.ts').read_text('utf-8')
    assert f'"{payload["workflows_version"]}"' in version, (
        'curation-cells.json was generated from a different WORKFLOWS_VERSION than version.ts '
        'declares; regenerate it with `bun run cells` in apps/web'
    )
