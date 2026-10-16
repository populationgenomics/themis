"""A round's records are read back field by field, and anything but what the loop wrote is refused."""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from tools.run_review import rounds

SETTINGS = rounds.Settings(
    gcp_project='cpg-themis-dev',
    service_account='themis-clu@cpg-themis-dev.iam.gserviceaccount.com',
    web_origin='https://app.example.org',
    workbench_project='prj_1',
    deadline_seconds=7200,
    settle_seconds=180,
    prepared_at='2026-10-05T09:30:00+00:00',
)


def test_the_settings_survive_the_round_record(tmp_path: pathlib.Path) -> None:
    rounds.create(tmp_path, SETTINGS)
    assert rounds.settings(tmp_path) == SETTINGS


@pytest.mark.parametrize(
    ('change', 'reason'),
    [
        ({'deadline_seconds': '7200'}, '`deadline_seconds` is an integer'),
        ({'deadline_seconds': True}, '`deadline_seconds` is an integer'),
        ({'web_origin': None}, '`web_origin` is a string'),
        ({'written_by_a_newer_harness': 1}, 'fields this harness does not read'),
    ],
    ids=['string for integer', 'bool for integer', 'null for string', 'unknown field'],
)
def test_a_round_record_of_another_shape_is_refused(
    tmp_path: pathlib.Path, change: dict[str, object], reason: str
) -> None:
    (tmp_path / rounds.ROUND_RECORD).write_text(json.dumps({**dataclasses.asdict(SETTINGS), **change}), 'utf-8')
    with pytest.raises(rounds.RoundError, match=reason):
        rounds.settings(tmp_path)


def test_a_round_record_missing_a_field_is_refused(tmp_path: pathlib.Path) -> None:
    document = dataclasses.asdict(SETTINGS)
    del document['settle_seconds']
    (tmp_path / rounds.ROUND_RECORD).write_text(json.dumps(document), 'utf-8')
    with pytest.raises(rounds.RoundError, match='has no `settle_seconds`'):
        rounds.settings(tmp_path)


def test_a_record_that_is_not_an_object_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / rounds.ROUND_RECORD).write_text('[1, 2]', 'utf-8')
    with pytest.raises(rounds.RoundError, match='is not a JSON object'):
        rounds.settings(tmp_path)
