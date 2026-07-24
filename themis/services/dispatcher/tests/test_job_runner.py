"""Tests for the sandbox-spawn container override — the single trusted worker container (§4)."""

from __future__ import annotations

import json

from themis.services.dispatcher import job_runner

_REQUEST = job_runner.SpawnRequest(
    session_id='sid',
    work_id='wid',
    environment_id='eid',
    environment_key='ENV-KEY',
    session_token='SESSION-TOKEN',
)


def _by_name() -> dict[str, dict[str, str]]:
    return {c.name: c.env for c in job_runner._container_overrides(_REQUEST)}


def test_session_ids_reach_the_worker() -> None:
    worker = _by_name()['worker']
    assert worker['ANTHROPIC_SESSION_ID'] == 'sid'
    assert worker['ANTHROPIC_WORK_ID'] == 'wid'
    assert worker['ANTHROPIC_ENVIRONMENT_ID'] == 'eid'


def test_env_key_and_token_reach_the_worker() -> None:
    # The worker is trusted (untrusted code runs only inside the postern sandbox), so it holds both
    # credentials; there is no co-resident untrusted container to keep them from (§4).
    worker = _by_name()['worker']
    assert worker['ANTHROPIC_ENVIRONMENT_KEY'] == 'ENV-KEY'
    assert worker['THEMIS_SESSION_TOKEN'] == 'SESSION-TOKEN'


def test_body_is_the_run_overrides_shape() -> None:
    body = json.loads(json.dumps(job_runner._overrides_body(_REQUEST)))
    overrides = body['overrides']
    # The container named explicitly — an empty/omitted name would target the manifest's default.
    assert [c['name'] for c in overrides['containerOverrides']] == ['worker']
    assert overrides['taskCount'] == 1
