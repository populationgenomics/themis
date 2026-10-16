"""The commands against a round on disk: what they refuse before reaching anything, and what they report."""

from __future__ import annotations

import dataclasses
import pathlib
import signal
import subprocess
from collections.abc import Iterator

import pytest
import yaml

from tools.run_review import __main__ as cli
from tools.run_review import rounds
from tools.run_review.run import runner
from tools.run_review.tests import curation_rows
from tools.run_review.variant_classification import reference, scenario

ROUND = '2026-10-05-0930'
SETTINGS = rounds.Settings(
    gcp_project='cpg-themis-dev',
    service_account='themis-clu@cpg-themis-dev.iam.gserviceaccount.com',
    web_origin='https://app.example.org',
    workbench_project='prj_1',
    deadline_seconds=7200,
    settle_seconds=180,
    prepared_at='2026-10-05T09:30:00+00:00',
)


@pytest.fixture(autouse=True)
def rounds_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(rounds, 'ROUNDS_DIR', tmp_path / 'rounds')
    return tmp_path / 'rounds'


@pytest.fixture(autouse=True)
def restored_signal_handlers() -> Iterator[None]:
    """`main` maps SIGTERM and SIGHUP to Ctrl-C's handler; the test process keeps its own."""
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for signum, handler in saved.items():
        signal.signal(signum, handler)


def _round(rounds_root: pathlib.Path) -> pathlib.Path:
    directory = rounds_root / ROUND
    directory.mkdir(parents=True)
    rounds.create(directory, SETTINGS)
    return directory


def _variant(directory: pathlib.Path, variant_id: str, record: runner.RunRecord | None) -> pathlib.Path:
    variant_dir = directory / variant_id
    stored = dataclasses.replace(
        curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows())),
        registration=dataclasses.replace(curation_rows.REGISTRATION, variant_id=variant_id),
    )
    rounds.write_json(variant_dir / scenario.REFERENCE, reference.to_json(stored))
    rounds.write_json(variant_dir / scenario.KICKOFF, {'inputs': {}})
    if record is not None:
        rounds.write_json(variant_dir / rounds.RUN_DIR / runner.RUN_RECORD, dataclasses.asdict(record))
    return variant_dir


def _record(**changes: object) -> runner.RunRecord:
    created = runner.Created(analysis_id='an_1', created_at='2026-10-05T09:31:00+00:00', deadline_at='x')
    record = runner.RunRecord(inputs={}, created=created)
    return dataclasses.replace(record, **changes)


def _collected(agent_version: int) -> runner.RunRecord:
    return _record(
        collected_at='2026-10-05T10:30:00+00:00',
        provenance={'agent_id': 'agent_1', 'agent_version': agent_version, 'skills': []},
    )


def test_status_names_a_runs_failure_and_why_it_was_not_collected(
    rounds_root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _round(rounds_root)
    _variant(directory, 'v1', _record(failure='RunError: the BFF answered nothing'))
    _variant(directory, 'v2', _record(collection_error='git clone failed'))

    assert cli.main(['status', '--round', ROUND]) == 0

    out = capsys.readouterr().out
    assert 'v1: analysis an_1; ending pending; FAILED: RunError: the BFF answered nothing' in out
    assert 'v2: analysis an_1; ending pending; NOT COLLECTED: git clone failed' in out


def test_status_names_the_rerun_for_runs_on_an_older_agent_version(
    rounds_root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _round(rounds_root)
    _variant(directory, 'v1', _collected(7))
    _variant(directory, 'v2', _collected(8))
    _variant(directory, 'v3', _collected(8))

    assert cli.main(['status', '--round', ROUND]) == 1

    assert '`run --rerun v1`' in capsys.readouterr().out


def test_prepare_refuses_a_settle_period_below_the_floor(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['prepare', '--round', ROUND, '--workbench-project', 'p', '--settle-seconds', '60']) == 1
    assert '--settle-seconds is at least' in capsys.readouterr().err


def test_prepare_refuses_a_project_whose_stack_is_not_dev(
    rounds_root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ['prepare', '--round', ROUND, '--workbench-project', 'p', '--project', 'cpg-themis-prod']
    assert cli.main(argv) == 1
    assert 'the loop runs against dev only' in capsys.readouterr().err
    assert not rounds_root.exists()


def test_prepare_refuses_a_round_that_exists(rounds_root: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    (rounds_root / ROUND).mkdir(parents=True)
    assert cli.main(['prepare', '--round', ROUND, '--workbench-project', 'p']) == 1
    assert 'exists; name a new round' in capsys.readouterr().err


def _stage_and_fail(directory: pathlib.Path) -> None:
    with rounds.staged(directory) as stage:
        (stage / 'v1').mkdir()
        raise RuntimeError('the read failed partway')


def test_a_round_staged_by_a_prepare_that_fails_leaves_nothing(rounds_root: pathlib.Path) -> None:
    with pytest.raises(RuntimeError):
        _stage_and_fail(rounds_root / ROUND)
    assert list(rounds_root.iterdir()) == []


def test_a_round_staged_whole_is_renamed_into_place(rounds_root: pathlib.Path) -> None:
    directory = rounds_root / ROUND
    with rounds.staged(directory) as stage:
        rounds.create(stage, SETTINGS)
    assert rounds.settings(directory) == SETTINGS
    assert [path.name for path in rounds_root.iterdir()] == [ROUND]


def test_render_records_a_variant_that_fails_and_renders_the_rest(
    rounds_root: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _round(rounds_root)
    rendered = _variant(directory, 'v1', _collected(7))
    broken = _variant(directory, 'v2', _collected(7))
    for variant_dir in (rendered, broken):
        repository = variant_dir / rounds.RUN_DIR / runner.REPOSITORY
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)  # noqa: S603, S607
    rounds.write_json(broken / scenario.REFERENCE, {'registration': {}, 'submissions': []})
    uncollected = _variant(directory, 'v3', _record())
    (uncollected / scenario.RENDERED_DIR).mkdir()
    (uncollected / scenario.RENDERED_DIR / scenario.RENDERED_RECORD).write_text('from a superseded run\n', 'utf-8')
    stale = rendered / scenario.RENDERED_DIR / 'curator-Z.yaml'
    stale.parent.mkdir()
    stale.write_text('from an earlier rendering\n', 'utf-8')

    assert cli.main(['render', '--round', ROUND]) == 1

    assert not (uncollected / scenario.RENDERED_DIR).exists()
    assert not stale.exists()

    for name in (scenario.RENDERED_RECORD, scenario.RENDERED_REGISTRATION, scenario.rendered_submission('A')):
        assert isinstance(yaml.safe_load((rendered / scenario.RENDERED_DIR / name).read_text('utf-8')), dict)
    assert 'has no `variant_id`' in (broken / 'render-error.txt').read_text('utf-8')
    err = capsys.readouterr().err
    assert 'v2: RENDER FAILED' in err
    assert 'v3: NOT RENDERED, its run was not collected' in err
