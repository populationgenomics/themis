"""One spelling of a protein change: equivalent spellings compare equal."""

from __future__ import annotations

import pytest
import weaver

from themis.litcache import variant_validate


def _change(expression: str) -> str:
    return variant_validate.protein_change(weaver.parse(expression))


@pytest.mark.parametrize(
    'spellings',
    [
        ('NP_1.1:p.R337H', 'NP_1.1:p.Arg337His', 'NP_1.1:p.(Arg337His)'),
        ('NP_1.1:p.Gln458*', 'NP_1.1:p.Gln458Ter', 'NP_1.1:p.(Gln458*)'),
        ('NP_1.1:p.Gly2154fs', 'NP_1.1:p.Gly2154fsTer78', 'NP_1.1:p.G2154fs*78', 'NP_1.1:p.(Gly2154fs*?)'),
    ],
)
def test_spellings_of_one_protein_change_share_a_key(spellings: tuple[str, ...]) -> None:
    assert len({_change(s) for s in spellings}) == 1


def test_different_changes_keep_different_keys() -> None:
    assert _change('NP_1.1:p.Arg337His') != _change('NP_1.1:p.Arg337Cys')
    assert _change('NP_1.1:p.Gly2154fs') != _change('NP_1.1:p.Gly2154Alafs*78')
