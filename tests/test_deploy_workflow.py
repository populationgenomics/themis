"""The deploy workflow records how every run ended, after the steps that deploy, and only its deploy job mints a token.

The deploy-failure alert holds while a run's failure is newer than every success, so a run that records only its
failures would open an incident nothing closes, and one that records its success before a later step fails would close
it early. The check jobs run on every prod deploy and hold no `id-token` permission: the deploy SA's binding admits
this workflow, so any job in it that can mint a token could impersonate the SA.
"""

from __future__ import annotations

import pathlib

import yaml

_WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'deploy.yml'
_RECORDER = 'python -m themis.telemetry.deploy_outcome'


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _jobs() -> dict[str, object]:
    return _mapping(yaml.safe_load(_WORKFLOW.read_text('utf-8'))['jobs'])


def _steps(job: object) -> list[dict[str, object]]:
    steps = _mapping(job).get('steps', [])
    assert isinstance(steps, list), steps
    return [_mapping(step) for step in steps]


def test_the_outcome_is_recorded_once_as_the_deploy_jobs_last_step() -> None:
    recorders = [
        (name, index)
        for name, job in _jobs().items()
        for index, step in enumerate(_steps(job))
        if _RECORDER in str(step.get('run', ''))
    ]
    assert recorders == [('deploy', len(_steps(_jobs()['deploy'])) - 1)]


def test_the_outcome_step_runs_whatever_the_job_did_and_records_its_status() -> None:
    last = _steps(_jobs()['deploy'])[-1]
    # `always()` runs it after a failed or cancelled step too; the result is the job's own status at that point.
    assert str(last['if']).startswith('always()'), last['if']
    assert 'job.status' in str(_mapping(last['env'])['RESULT']), last['env']


def test_only_the_deploy_job_can_mint_an_id_token() -> None:
    holders = [name for name, job in _jobs().items() if 'id-token' in _mapping(_mapping(job).get('permissions', {}))]
    assert holders == ['deploy']
