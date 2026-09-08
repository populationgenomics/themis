"""The report reads exactly what the infrastructure renders, and fails on anything else naming the path.

The round trip is the contract test between the two sides: `themis_infra.cost_report.report_json` is what the Pulumi
program puts into the Job's environment, and `report_queries.parse` is what the Job reads out of it. Production code
never imports `themis_infra`; this test reaches it through pytest's `pythonpath`, which puts `infra` on the path.
Unlike `test_metric_contract.py`, the module cannot be loaded by file path alone: it imports the program's other
modules by package name.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import pytest

from themis.services.cost_exporter import report_queries
from themis_infra import cost_report


def test_the_rendered_queries_round_trip() -> None:
    text = cost_report.report_json()

    parsed = report_queries.parse(text)

    assert dataclasses.asdict(parsed) == json.loads(text)


def test_the_two_sides_name_the_same_keys() -> None:
    rendered = json.loads(cost_report.report_json())
    assert set(rendered['day']) == set(report_queries.DAY_KEYS)
    assert set(rendered['hourly']) == set(report_queries.HOURLY_KEYS)


def _document() -> dict[str, dict[str, object]]:
    return {
        'day': {key: f'day {key}' for key in report_queries.DAY_KEYS},
        'hourly': {key: f'hourly {key}' for key in report_queries.HOURLY_KEYS},
    }


def _missing_day_key(document: dict[str, dict[str, object]]) -> None:
    del document['day']['sessions_tokens']


def _extra_day_key(document: dict[str, dict[str, object]]) -> None:
    document['day']['sessions_by_agent'] = 'q'


def _missing_hourly(document: dict[str, dict[str, object]]) -> None:
    del document['hourly']


def _extra_root_key(document: dict[str, dict[str, object]]) -> None:
    document['weekly'] = {}


def _empty_query(document: dict[str, dict[str, object]]) -> None:
    document['hourly']['ci'] = ''


def _numeric_query(document: dict[str, dict[str, object]]) -> None:
    document['day']['convert'] = 7


def _day_a_list(document: dict[str, dict[str, object]]) -> None:
    document['day'] = []  # type: ignore[assignment]


@pytest.mark.parametrize(
    ('mutate', 'complaint'),
    [
        pytest.param(_missing_day_key, r"queries\.day: missing \['sessions_tokens'\]", id='missing-day-key'),
        pytest.param(_extra_day_key, r"queries\.day: unexpected \['sessions_by_agent'\]", id='extra-day-key'),
        pytest.param(_missing_hourly, r"queries: missing \['hourly'\]", id='missing-hourly'),
        pytest.param(_extra_root_key, r"queries: unexpected \['weekly'\]", id='extra-root-key'),
        pytest.param(_empty_query, r'queries\.hourly\.ci: expected a non-empty string', id='empty-query'),
        pytest.param(_numeric_query, r'queries\.day\.convert: expected a non-empty string, got 7', id='not-a-string'),
        pytest.param(_day_a_list, r'queries\.day: expected an object, got list', id='day-not-an-object'),
    ],
)
def test_a_document_that_is_not_the_rendered_shape_fails_naming_the_path(
    mutate: Callable[[dict[str, dict[str, object]]], None], complaint: str
) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(ValueError, match=complaint):
        report_queries.parse(json.dumps(document))


@pytest.mark.parametrize(
    ('text', 'complaint'),
    [
        pytest.param('{"day": {', 'not JSON', id='truncated'),
        pytest.param('[]', 'queries: expected an object, got list', id='not-an-object'),
    ],
)
def test_text_that_is_not_a_document_fails(text: str, complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        report_queries.parse(text)


def test_the_valid_document_parses() -> None:
    # The mutations above each break this document; that it parses rules out a vacuous rejection.
    parsed = report_queries.parse(json.dumps(_document()))
    assert parsed.day.sessions_tokens == 'day sessions_tokens'
    assert parsed.hourly.ci == 'hourly ci'
