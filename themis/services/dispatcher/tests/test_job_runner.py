"""Tests for the sandbox-spawn container overrides — the per-container credential targeting (§7)."""

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


def test_ids_reach_both_containers() -> None:
    containers = _by_name()
    for name in ('agent', 'proxy'):
        assert containers[name]['ANTHROPIC_SESSION_ID'] == 'sid'
        assert containers[name]['ANTHROPIC_WORK_ID'] == 'wid'
        assert containers[name]['ANTHROPIC_ENVIRONMENT_ID'] == 'eid'


def test_env_key_and_token_reach_the_proxy_only() -> None:
    containers = _by_name()
    assert containers['proxy']['ANTHROPIC_ENVIRONMENT_KEY'] == 'ENV-KEY'
    assert containers['proxy']['THEMIS_SESSION_TOKEN'] == 'SESSION-TOKEN'
    # The agent container runs untrusted code and must receive neither credential (§7).
    assert 'ANTHROPIC_ENVIRONMENT_KEY' not in containers['agent']
    assert 'THEMIS_SESSION_TOKEN' not in containers['agent']


def test_body_is_the_run_overrides_shape() -> None:
    body = json.loads(json.dumps(job_runner._overrides_body(_REQUEST)))
    overrides = body['overrides']
    # Each container named explicitly — an empty/omitted name could target the agent (§7).
    assert [c['name'] for c in overrides['containerOverrides']] == ['agent', 'proxy']
    assert overrides['taskCount'] == 1
