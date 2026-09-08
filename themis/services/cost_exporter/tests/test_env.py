"""Every setting is required, a miss exits naming it, and a deadline is a positive whole count of seconds."""

from __future__ import annotations

import pytest

from themis.services.cost_exporter import env


def test_a_set_variable_reads_as_its_value() -> None:
    assert env.require({'THEMIS_X': 'value'}, 'THEMIS_X') == 'value'
    assert env.positive_seconds({'THEMIS_X': '120'}, 'THEMIS_X') == 120


@pytest.mark.parametrize('environ', [{}, {'THEMIS_X': ''}], ids=['unset', 'empty'])
def test_an_unset_or_empty_variable_exits_naming_it(environ: dict[str, str]) -> None:
    with pytest.raises(SystemExit, match='THEMIS_X'):
        env.require(environ, 'THEMIS_X')


@pytest.mark.parametrize('value', ['0', '-5', '1.5', 'soon'])
def test_a_count_that_is_not_a_positive_whole_number_of_seconds_exits(value: str) -> None:
    with pytest.raises(SystemExit, match='THEMIS_X must be a positive whole number of seconds'):
        env.positive_seconds({'THEMIS_X': value}, 'THEMIS_X')
