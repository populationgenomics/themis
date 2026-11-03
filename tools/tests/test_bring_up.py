"""A first bring-up resumes where it stopped, and stops loudly where the operator has to act."""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
from collections.abc import Mapping, Sequence
from typing import override

import pytest
import yaml

from tools import bring_up, stack

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEPLOY_WORKFLOW = _ROOT / '.github' / 'workflows' / 'deploy.yml'
_ENV = bring_up.Environment(project='p', stack='prod', region='australia-southeast1', pulumi_project='themis')
_MAIN = 'a' * 40
_OLD = 'b' * 40
_VALID_ACL = json.dumps({'format': 1, 'projects': {}}).encode('utf-8')
_REAL = {
    'themis:iapOauthClientId': '1-a.apps.googleusercontent.com',
    'themis:anthropicDeployFederationRuleId': 'fdrl_1',
    'themis:anthropicDeployServiceAccountId': 'svac_1',
}


@dataclasses.dataclass
class _Run:
    id: int
    title: str
    sha: str
    status: str = 'completed'
    failed: tuple[str, ...] = ()


class _World(bring_up.Shell):
    """GitHub, the registry, the refresh job and the ACL secret as one fake; any other command fails the test."""

    def __init__(self) -> None:
        self.main = _MAIN
        self.head = _MAIN
        self.dirty = ''
        self.history: list[dict[str, object]] = [{'result': 'succeeded', 'environment': {'exec.kind': 'cli'}}]
        self.pushed: set[str] = set()
        self.refreshed = False
        self.acl: list[bytes | None] = [_VALID_ACL]
        self.runs: list[_Run] = []
        self.deploy_fails_in: tuple[str, ...] = ()
        self.commands: list[list[str]] = []

    @override
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: pathlib.Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: datetime.timedelta | None = None,
        echo: bool = False,
    ) -> bytes:
        del cwd, env, timeout, echo
        command = list(args)
        self.commands.append(command)
        answer = self._answer(command)
        return answer.encode('utf-8') if isinstance(answer, str) else answer

    def _answer(self, command: list[str]) -> str | bytes:  # noqa: PLR0911 — the fake answers each command in a branch
        if command[:2] == ['gh', 'api']:
            return self.main
        if command[:2] == ['git', 'rev-parse']:
            return self.head
        if command[:2] == ['git', 'status']:
            return self.dirty
        if 'pulumi' in command and 'history' in command:
            return json.dumps(self.history)
        if 'pulumi' in command and 'up' in command:
            return ''
        if command[:4] == ['gcloud', 'artifacts', 'docker', 'images']:
            if command[5] not in self.pushed:
                raise bring_up.CommandError(command, 1, 'ERROR: NOT_FOUND: Requested entity was not found.')
            return ''
        if command[:5] == ['gcloud', 'run', 'jobs', 'executions', 'list']:
            return 'execution-1' if self.refreshed else ''
        if command[:4] == ['gcloud', 'run', 'jobs', 'execute']:
            self.refreshed = True
            return ''
        if command[:4] == ['gcloud', 'secrets', 'versions', 'access']:
            version = self.acl.pop(0) if len(self.acl) > 1 else self.acl[0]
            if version is None:
                raise bring_up.CommandError(command, 1, 'NOT_FOUND: Secret has no versions.')
            return version
        if command[:3] == ['gh', 'workflow', 'run']:
            return self._dispatched(images_only='images_only=true' in command)
        if command[:3] == ['gh', 'run', 'list']:
            listed = [
                {'databaseId': r.id, 'displayTitle': r.title, 'headSha': r.sha, 'status': r.status, 'conclusion': ''}
                for r in self.runs
            ]
            return json.dumps(listed)
        if command[:3] == ['gh', 'run', 'watch']:
            (watched,) = [r for r in self.runs if str(r.id) == command[3]]
            watched.status = 'completed'
            if watched.title.endswith('(images only)'):
                self.pushed |= set(bring_up.built_images(_ENV, watched.sha).values())
            return ''
        if command[:3] == ['gh', 'run', 'view']:
            (found,) = [r for r in self.runs if str(r.id) == command[3]]
            steps = [{'name': name, 'conclusion': 'failure'} for name in found.failed]
            return json.dumps({'conclusion': 'failure' if found.failed else 'success', 'jobs': [{'steps': steps}]})
        raise AssertionError(f'unanticipated command: {command}')

    def _dispatched(self, *, images_only: bool) -> str:
        title = f'deploy {_ENV.stack}{" (images only)" if images_only else ""}'
        failed = () if images_only else self.deploy_fails_in
        self.runs.append(_Run(id=100 + len(self.runs), title=title, sha=self.main, status='queued', failed=failed))
        return ''

    @override
    def sleep(self, delay: datetime.timedelta) -> None:
        del delay

    def ran(self, *words: str) -> bool:
        return any(all(word in command for word in words) for command in self.commands)

    def dispatches(self, images_only: bool) -> int:
        flag = f'images_only={str(images_only).lower()}'
        return sum(1 for command in self.commands if command[:3] == ['gh', 'workflow', 'run'] and flag in command)


@pytest.fixture
def values(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The stack values the run reads; a test edits the mapping to set one."""
    stated = dict(_REAL)
    monkeypatch.setattr(stack, 'value', lambda _project, key, **_kwargs: stated[key])
    return stated


def _bring_up(world: _World, *, acl_wait: datetime.timedelta = datetime.timedelta(minutes=5)) -> None:
    bring_up.BringUp(_ENV, world, acl_wait=acl_wait).run()


def _targeted(world: _World, target: str) -> bool:
    return any('up' in command and target in command for command in world.commands)


@pytest.mark.usefixtures('values')
def test_a_fresh_stack_is_brought_up_through_every_step() -> None:
    world = _World()
    _bring_up(world)
    assert _targeted(world, bring_up.first_targets(_ENV)[0])
    assert world.dispatches(images_only=True) == 1
    assert _targeted(world, bring_up.refresh_targets(_ENV)[0])
    assert world.ran('jobs', 'execute')
    assert any('up' in command and '--target' not in command for command in world.commands)
    assert world.dispatches(images_only=False) == 1


@pytest.mark.usefixtures('values')
def test_a_resumed_run_repeats_no_result_it_finds() -> None:
    world = _World()
    world.pushed = set(bring_up.built_images(_ENV, _MAIN).values())
    world.refreshed = True
    _bring_up(world)
    assert world.dispatches(images_only=True) == 0
    assert not world.ran('jobs', 'execute')


@pytest.mark.usefixtures('values')
def test_images_for_an_older_commit_are_built_again_for_main() -> None:
    world = _World()
    world.pushed = set(bring_up.built_images(_ENV, _OLD).values())
    _bring_up(world)
    assert world.dispatches(images_only=True) == 1
    assert set(bring_up.built_images(_ENV, _MAIN).values()) <= world.pushed


@pytest.mark.usefixtures('values')
def test_a_run_already_going_for_the_commit_is_waited_for_not_dispatched_again() -> None:
    world = _World()
    world.runs.append(_Run(id=7, title='deploy prod (images only)', sha=_MAIN, status='in_progress'))
    _bring_up(world)
    assert world.dispatches(images_only=True) == 0
    assert set(bring_up.built_images(_ENV, _MAIN).values()) <= world.pushed


@pytest.mark.parametrize(('head', 'dirty'), [(_OLD, ''), (_MAIN, ' M infra/__main__.py')])
@pytest.mark.usefixtures('values')
def test_a_checkout_other_than_a_clean_main_is_refused(head: str, dirty: str) -> None:
    world = _World()
    world.head, world.dirty = head, dirty
    with pytest.raises(bring_up.BringUpError, match='same commit'):
        _bring_up(world)
    assert not world.ran('up')


def test_a_stack_the_workflow_has_updated_is_refused() -> None:
    world = _World()
    world.history = [{'result': 'succeeded', 'environment': {'ci.system': 'GitHub Actions'}}]
    with pytest.raises(bring_up.BringUpError, match='first bring-up is over'):
        _bring_up(world)
    assert not world.ran('up')


def test_a_failed_workflow_update_does_not_end_the_bring_up() -> None:
    assert not bring_up.deployed_by_the_workflow([{'result': 'failed', 'environment': {'ci.system': 'GitHub Actions'}}])


def test_the_full_up_waits_for_a_real_oauth_client(values: dict[str, str]) -> None:
    values['themis:iapOauthClientId'] = 'placeholder'
    world = _World()
    with pytest.raises(bring_up.BringUpError, match='iapOauthClientId'):
        _bring_up(world)
    assert not [command for command in world.commands if 'up' in command and '--target' not in command]


@pytest.mark.parametrize('version', [b'{"format": 1}', b'\xff\xfe'])
@pytest.mark.usefixtures('values')
def test_an_invalid_acl_stops_before_the_full_up(version: bytes) -> None:
    world = _World()
    world.acl = [version]
    with pytest.raises(bring_up.BringUpError, match='not a valid ACL'):
        _bring_up(world)
    assert not [command for command in world.commands if 'up' in command and '--target' not in command]


@pytest.mark.usefixtures('values')
def test_the_run_waits_for_the_sites_first_acl_version() -> None:
    world = _World()
    world.acl = [None, None, _VALID_ACL]
    _bring_up(world)
    assert world.dispatches(images_only=False) == 1


@pytest.mark.usefixtures('values')
def test_a_missing_acl_stops_at_the_deadline() -> None:
    world = _World()
    world.acl = [None]
    with pytest.raises(bring_up.BringUpError, match='no readable version'):
        _bring_up(world, acl_wait=datetime.timedelta(0))


def test_the_agent_step_may_fail_while_the_deploy_identity_is_a_placeholder(values: dict[str, str]) -> None:
    values['themis:anthropicDeployServiceAccountId'] = 'placeholder'
    world = _World()
    world.deploy_fails_in = (bring_up.AGENT_STEP,)
    _bring_up(world)


@pytest.mark.usefixtures('values')
def test_the_agent_step_failing_with_a_real_deploy_identity_stops_the_bring_up() -> None:
    world = _World()
    world.deploy_fails_in = (bring_up.AGENT_STEP,)
    with pytest.raises(bring_up.BringUpError, match=bring_up.AGENT_STEP):
        _bring_up(world)


def test_any_other_failing_step_stops_the_bring_up(values: dict[str, str]) -> None:
    values['themis:anthropicDeployServiceAccountId'] = 'placeholder'
    world = _World()
    world.deploy_fails_in = ('Apply database migrations',)
    with pytest.raises(bring_up.BringUpError, match='Apply database migrations'):
        _bring_up(world)


def test_a_check_that_fails_for_another_reason_than_absence_is_loud() -> None:
    class _Denied(_World):
        @override
        def _answer(self, command: list[str]) -> str | bytes:
            raise bring_up.CommandError(command, 1, 'PERMISSION_DENIED: the caller lacks artifactregistry.get')

    with pytest.raises(bring_up.CommandError, match='PERMISSION_DENIED'):
        _Denied().exists(['gcloud', 'artifacts', 'docker', 'images', 'describe', 'x', '--quiet'])


def test_every_image_override_is_set_on_every_up() -> None:
    declared = {image['env'] for image in bring_up.images()}
    assert declared
    assert set(bring_up.placeholder_images()) == declared
    assert set(bring_up.built_images(_ENV, _MAIN)) == declared


def _workflow() -> dict[object, object]:
    return yaml.safe_load(_DEPLOY_WORKFLOW.read_text('utf-8'))


def test_every_stack_is_an_environment_the_workflow_deploys() -> None:
    # PyYAML reads the bare `on` key as True.
    inputs = _workflow()[True]['workflow_dispatch']['inputs']  # pyright: ignore[reportIndexIssue]
    assert {'environment', 'images_only'} <= set(inputs)
    files = (_ROOT / 'infra').glob('Pulumi.*.yaml')
    stacks = {path.name.removeprefix('Pulumi.').removesuffix('.yaml') for path in files}
    assert stacks
    assert stacks <= set(inputs['environment']['options'])


def test_the_tolerated_step_is_a_step_of_the_deploy_workflow() -> None:
    jobs = _workflow()['jobs']
    # A job that calls a reusable workflow has no steps of its own.
    names = {step.get('name') for job in jobs.values() for step in job.get('steps', ())}  # pyright: ignore[reportAttributeAccessIssue]
    assert bring_up.AGENT_STEP in names
