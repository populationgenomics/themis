"""A fresh environment's first bring-up, run once by a project Owner from a laptop.

    uv run python -m tools.bring_up --project cpg-themis-prod

`docs/runbooks/fresh-environment.md` says what has to exist first and what follows;
`docs/design/deployment.md` (Bringing up a fresh environment) why the steps run in this order. In short:
the registry and the ACL secret first, the images built by the deploy workflow, the gene-disease dumps
written once, the site's ACL waited for, then the full `up` as the Owner, and a full deploy through the
workflow, which applies the migrations.

The program the Owner runs and the images the workflow builds come from one commit: the run refuses a
checkout that is not a clean copy of `main`'s head. Every step either converges (the targeted `up`s, which
are no-ops once applied) or checks the result it would produce first, so a run that stops resumes where it
stopped. The tool refuses a stack the deploy workflow has already updated: from then on the environment is
deployed through the workflow.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

import yaml

from themis.services.auth import acl
from tools import stack

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INFRA = _ROOT / 'infra'
_IMAGES = _ROOT / '.github' / 'images.json'
_REPOSITORY = 'populationgenomics/themis-internal'
_WORKFLOW = 'deploy.yml'
_PLACEHOLDER = 'placeholder'
_PLACEHOLDER_IMAGE = 'gcr.io/cloudrun/hello'
REFRESH_JOB = 'themis-gene-disease-refresh'
# The Secret Manager secret the site writes each version of the ACL into.
_ACL_NAME = 'acl'
# The deploy workflow's step that applies the agent on the Anthropic side: it fails while the deploy
# account's Anthropic identity is still a placeholder.
AGENT_STEP = 'Apply the agent declaration'
_DEPLOY_IDENTITY_KEYS = ('themis:anthropicDeployFederationRuleId', 'themis:anthropicDeployServiceAccountId')

_IMAGES_DEADLINE = datetime.timedelta(minutes=60)
_DEPLOY_DEADLINE = datetime.timedelta(minutes=90)
_REFRESH_DEADLINE = datetime.timedelta(minutes=150)
_RUN_APPEARS_DEADLINE = datetime.timedelta(minutes=3)
_POLL = datetime.timedelta(seconds=30)


class BringUpError(Exception):
    """A step cannot go on; the message says what the operator has to do."""


class CommandError(BringUpError):
    """A command exited non-zero."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        detail = f': {stderr.strip()}' if stderr.strip() else ''
        super().__init__(f'{" ".join(args)} exited {returncode}{detail}')
        self.stderr = stderr


class CommandTimeoutError(BringUpError):
    """A command outlived its deadline."""


class Shell:
    """Runs the `git`, `pulumi`, `gcloud` and `gh` commands the steps need; a test replaces it."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: pathlib.Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: datetime.timedelta | None = None,
        echo: bool = False,
    ) -> bytes:
        """Run `args` and return its stdout; with `echo`, stream its stdout to the terminal and return nothing.

        Raises:
            CommandError: If the command exits non-zero; the error carries its stderr.
            CommandTimeoutError: If it outlives `timeout`.
        """
        try:
            completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
                list(args),
                cwd=cwd,
                env={**os.environ, **(env or {})},
                timeout=None if timeout is None else timeout.total_seconds(),
                check=False,
                stdout=None if echo else subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired as e:
            raise CommandTimeoutError(f'{" ".join(args)} did not finish within {timeout}') from e
        if completed.returncode != 0:
            raise CommandError(args, completed.returncode, completed.stderr.decode('utf-8', 'replace'))
        return b'' if echo else completed.stdout

    def exists(self, args: Sequence[str]) -> bool:
        """Whether the thing `args` describes exists: a `NOT_FOUND` answer is absence, any other failure an error.

        Raises:
            CommandError: If the command fails for any reason but the thing's absence.
        """
        try:
            self.run(args)
        except CommandError as e:
            if 'NOT_FOUND' in e.stderr or 'not found' in e.stderr.lower():
                return False
            raise
        return True

    def sleep(self, delay: datetime.timedelta) -> None:
        time.sleep(delay.total_seconds())


@dataclasses.dataclass(frozen=True)
class Environment:
    """The one environment a run brings up, as its committed stack file states it."""

    project: str
    stack: str
    region: str
    pulumi_project: str

    @property
    def backend(self) -> str:
        return f'gs://{self.project}-pulumi-state'

    @property
    def image_prefix(self) -> str:
        return f'{self.region}-docker.pkg.dev/{self.project}/themis'

    def urn(self, type_chain: str, name: str = '') -> str:
        """A resource's URN in this stack; with no `name`, the target for everything under `type_chain`."""
        prefix = f'urn:pulumi:{self.stack}::{self.pulumi_project}::{type_chain}'
        return f'{prefix}::{name}' if name else f'{prefix}**'


def environment(project: str) -> Environment:
    """The environment `project` deploys, from its committed stack file."""
    region = stack.value(project, 'gcp:region')
    pulumi_project = yaml.safe_load((_INFRA / 'Pulumi.yaml').read_text('utf-8'))['name']
    return Environment(project=project, stack=stack.name(project), region=region, pulumi_project=pulumi_project)


def first_targets(env: Environment) -> list[str]:
    """The registry and the ACL secret: everything a run needs before any image or the site's ACL exists.

    The registry depends on every API the baseline enables, so the whole baseline comes with it. The ACL
    secret and the auth service's account go now so the site can write the ACL while the images build, and
    the deploy account's registry role so the workflow can push them.
    """
    return [
        env.urn('themis:infra:Baseline'),
        env.urn('themis:infra:AclSecret'),
        env.urn('gcp:serviceaccount/account:Account', 'themis-auth-runtime'),
        env.urn('themis:grants:DeployAccountBuilder', 'themis-deploy-builds-the-project'),
        env.urn(
            'themis:grants:DeployAccountBuilder$gcp:projects/iAMMember:IAMMember',
            'themis-deploy-artifactregistry-admin',
        ),
    ]


def refresh_targets(env: Environment) -> list[str]:
    """The gene-disease refresh job and what it writes to: the evidence service cannot start without its dumps."""
    grant = 'themis-gene-disease-refresh-writes-resources'
    return [
        env.urn('gcp:storage/bucket:Bucket', 'themis-resources'),
        env.urn('gcp:serviceaccount/account:Account', 'themis-gene-disease-refresh-runtime'),
        env.urn('gcp:cloudrunv2/job:Job', 'themis-gene-disease-refresh-job'),
        env.urn('themis:grants:BucketObjectReadWriter', grant),
        env.urn('themis:grants:BucketObjectReadWriter$gcp:storage/bucketIAMMember:BucketIAMMember', grant),
    ]


def images() -> list[dict[str, str]]:
    """The service and job images the deploy workflow builds, as `.github/images.json` declares them."""
    declared = json.loads(_IMAGES.read_text('utf-8'))
    if not declared:
        raise BringUpError(f'{_IMAGES} declares no images')
    return declared


def placeholder_images() -> dict[str, str]:
    """Every image override set to a placeholder: a targeted `up` still evaluates the whole program."""
    return {image['env']: _PLACEHOLDER_IMAGE for image in images()}


def built_images(env: Environment, sha: str) -> dict[str, str]:
    """Every image override set to the image the deploy workflow pushed for commit `sha`."""
    return {image['env']: f'{env.image_prefix}/{image["name"]}:{sha}' for image in images()}


def deployed_by_the_workflow(history: Sequence[Mapping[str, object]]) -> bool:
    """Whether the workflow ever updated the stack: Pulumi records the CI system with every update it ran."""
    for update in history:
        environment = update.get('environment')
        if update.get('result') == 'succeeded' and isinstance(environment, dict) and environment.get('ci.system'):
            return True
    return False


class BringUp:
    """One run against one environment."""

    def __init__(self, env: Environment, shell: Shell, *, acl_wait: datetime.timedelta) -> None:
        self._env = env
        self._shell = shell
        self._acl_wait = acl_wait

    def run(self) -> None:
        """Run every step, in order; each converges or checks its own result first."""
        self._refuse_a_deployed_stack()
        sha = self._checkout_is_main()
        self._targeted_up(first_targets(self._env), placeholder_images())
        _say(f'The site can deploy the ACL into projects/{self._env.project}/secrets/{_ACL_NAME} now.')
        self._images(sha)
        self._targeted_up(refresh_targets(self._env), built_images(self._env, sha))
        self._refresh()
        self._wait_for_acl()
        self._full_up(sha)
        self._deploy(sha)
        _say(_NEXT.format(project=self._env.project))

    def _text(self, args: Sequence[str], *, cwd: pathlib.Path | None = None) -> str:
        return self._shell.run(args, cwd=cwd).decode('utf-8')

    def _pulumi(self, *args: str, images: Mapping[str, str] | None = None, echo: bool = False) -> str:
        env = {'PULUMI_BACKEND_URL': self._env.backend, **(images or {})}
        command = ['uv', 'run', '--locked', '--group', 'infra', 'pulumi', *args, '--stack', self._env.stack]
        return self._shell.run(command, cwd=_INFRA, env=env, echo=echo).decode('utf-8')

    def _refuse_a_deployed_stack(self) -> None:
        if deployed_by_the_workflow(json.loads(self._pulumi('stack', 'history', '--json'))):
            raise BringUpError(
                f'the deploy workflow has already updated stack {self._env.stack}, so its first bring-up is over; '
                f'deploy it by dispatching {_WORKFLOW}'
            )

    def _checkout_is_main(self) -> str:
        """`main`'s head, which this checkout has to be: the Owner's `up` and the workflow's images agree on it."""
        main = self._text(['gh', 'api', f'repos/{_REPOSITORY}/commits/main', '--jq', '.sha']).strip()
        head = self._text(['git', 'rev-parse', 'HEAD'], cwd=_ROOT).strip()
        dirty = self._text(['git', 'status', '--porcelain'], cwd=_ROOT).strip()
        if head != main or dirty:
            raise BringUpError(
                f'run from a clean checkout of main at {main[:12]} (this one is at {head[:12]}'
                f'{", with local changes" if dirty else ""}): the program this run applies and the images the '
                'workflow builds have to come from the same commit'
            )
        return main

    def _targeted_up(self, targets: Sequence[str], images: Mapping[str, str]) -> None:
        flags = [flag for target in targets for flag in ('--target', target)]
        self._pulumi('up', '--yes', *flags, images=images, echo=True)

    def _images(self, sha: str) -> None:
        if self._all_pushed(sha):
            _say(f'Every image exists for {sha[:12]}.')
            return
        _say(f'Building the images for {sha[:12]} through {_WORKFLOW} (images only).')
        self._run_workflow(sha, images_only=True, deadline=_IMAGES_DEADLINE)
        if not self._all_pushed(sha):
            raise BringUpError(f'the images-only run succeeded, but not every image is in {self._env.image_prefix}')

    def _all_pushed(self, sha: str) -> bool:
        return all(
            self._shell.exists(['gcloud', 'artifacts', 'docker', 'images', 'describe', image, '--quiet'])
            for image in built_images(self._env, sha).values()
        )

    def _refresh(self) -> None:
        location = [f'--region={self._env.region}', f'--project={self._env.project}']
        executions = self._text(
            [
                'gcloud',
                'run',
                'jobs',
                'executions',
                'list',
                f'--job={REFRESH_JOB}',
                *location,
                '--filter=status.succeededCount>0',
                '--format=value(metadata.name)',
            ]
        )
        if executions.strip():
            _say('The gene-disease dumps exist.')
            return
        _say('Running the gene-disease refresh once; the evidence service cannot start without its dumps.')
        self._shell.run(
            ['gcloud', 'run', 'jobs', 'execute', REFRESH_JOB, '--wait', *location],
            timeout=_REFRESH_DEADLINE,
            echo=True,
        )

    def _wait_for_acl(self) -> None:
        """Wait for the version the auth service reads, the latest, and validate it the way the service does."""
        secret = f'projects/{self._env.project}/secrets/{_ACL_NAME}'
        access = ['gcloud', 'secrets', 'versions', 'access', 'latest', f'--secret={_ACL_NAME}']
        waited = datetime.timedelta(0)
        while True:
            try:
                raw = self._shell.run([*access, f'--project={self._env.project}'])
                break
            except CommandError as e:
                if 'NOT_FOUND' not in e.stderr:
                    raise
            if waited >= self._acl_wait:
                raise BringUpError(
                    f'{secret} has no readable version after {self._acl_wait}; the auth service will not start '
                    'without one. Ask the site to deploy its ACL config for this environment, then run again.'
                )
            _say(f'Waiting for the site to write the ACL into {secret}.')
            self._shell.sleep(_POLL)
            waited += _POLL
        try:
            acl.parse(raw)
        except acl.AclError as e:
            raise BringUpError(
                f'the latest version of {secret} is not a valid ACL ({e}); the site has to fix it'
            ) from e
        _say('The ACL is in place and valid.')

    def _full_up(self, sha: str) -> None:
        if stack.value(self._env.project, 'themis:iapOauthClientId') == _PLACEHOLDER:
            raise BringUpError(
                'themis:iapOauthClientId is still a placeholder: create the OAuth client IAP signs people in through '
                '(docs/runbooks/access-control.md), set it and its secret in the stack file, then run again'
            )
        _say(f'Bringing up the whole environment on the images for {sha[:12]}.')
        self._pulumi('up', '--yes', images=built_images(self._env, sha), echo=True)

    def _deploy(self, sha: str) -> None:
        _say(f'Deploying {sha[:12]} through {_WORKFLOW}, which applies the migrations.')
        placeholders = [key for key in _DEPLOY_IDENTITY_KEYS if stack.value(self._env.project, key) == _PLACEHOLDER]
        tolerated = (AGENT_STEP,) if placeholders else ()
        if placeholders:
            _say(
                f'{", ".join(placeholders)} still read "{_PLACEHOLDER}", so the run fails at "{AGENT_STEP}" and the '
                'deploy-failed alert fires for it; the bring-up accepts that failure and no other.'
            )
        self._run_workflow(sha, images_only=False, deadline=_DEPLOY_DEADLINE, tolerated=tolerated)

    def _runs(self, title: str, sha: str) -> list[dict[str, object]]:
        listed = json.loads(
            self._text(
                [
                    'gh',
                    'run',
                    'list',
                    f'--repo={_REPOSITORY}',
                    f'--workflow={_WORKFLOW}',
                    '--branch=main',
                    '--event=workflow_dispatch',
                    '--limit=20',
                    '--json=databaseId,displayTitle,headSha,status,conclusion',
                ]
            )
        )
        return [run for run in listed if run['displayTitle'] == title and run['headSha'] == sha]

    def _run_workflow(
        self, sha: str, *, images_only: bool, deadline: datetime.timedelta, tolerated: Sequence[str] = ()
    ) -> None:
        """Dispatch the deploy workflow on `main` for `sha`, unless a run for it is already going, and wait for it."""
        title = f'deploy {self._env.stack}{" (images only)" if images_only else ""}'
        earlier = self._runs(title, sha)
        going = [run for run in earlier if run['status'] != 'completed']
        if going:
            run = str(going[0]['databaseId'])
            _say(f'A "{title}" run for {sha[:12]} is already going; waiting for it.')
        else:
            run = self._dispatch(title, sha, images_only=images_only, earlier={str(r['databaseId']) for r in earlier})
        self._wait_for(run, deadline, tolerated=tolerated)

    def _dispatch(self, title: str, sha: str, *, images_only: bool, earlier: set[str]) -> str:
        self._text(
            [
                'gh',
                'workflow',
                'run',
                _WORKFLOW,
                f'--repo={_REPOSITORY}',
                '--ref=main',
                '-f',
                f'environment={self._env.stack}',
                '-f',
                f'images_only={str(images_only).lower()}',
            ]
        )
        waited = datetime.timedelta(0)
        while True:
            new = [str(run['databaseId']) for run in self._runs(title, sha) if str(run['databaseId']) not in earlier]
            if new:
                return new[0]
            if waited >= _RUN_APPEARS_DEADLINE:
                raise BringUpError(
                    f'no "{title}" run for {sha[:12]} appeared within {_RUN_APPEARS_DEADLINE}; if main moved on, '
                    'check out its new head and run again'
                )
            self._shell.sleep(datetime.timedelta(seconds=5))
            waited += datetime.timedelta(seconds=5)

    def _wait_for(self, run: str, deadline: datetime.timedelta, *, tolerated: Sequence[str]) -> None:
        """Wait for `run` to finish.

        Raises:
            BringUpError: If it outlived `deadline`, or failed in a step outside `tolerated`.
        """
        url = f'https://github.com/{_REPOSITORY}/actions/runs/{run}'
        _say(f'Waiting for {url}.')
        try:
            self._shell.run(['gh', 'run', 'watch', run, f'--repo={_REPOSITORY}', '--interval=30'], timeout=deadline)
        except CommandTimeoutError as e:
            raise BringUpError(f'{url} did not finish within {deadline}') from e
        view = json.loads(self._text(['gh', 'run', 'view', run, f'--repo={_REPOSITORY}', '--json=conclusion,jobs']))
        if view['conclusion'] == 'success':
            return
        failed = [step['name'] for job in view['jobs'] for step in job['steps'] if step.get('conclusion') == 'failure']
        if not failed or any(step not in tolerated for step in failed):
            raise BringUpError(f'{url} ended {view["conclusion"]}, failing in {failed or "no step"}')
        _say(f'{url} failed only in {failed}, which this bring-up accepts.')


_NEXT = """
The first bring-up of {project} is done. What follows is outside this tool:
- Anthropic round 2: register the web app's, the cost exporter's and the convert worker's identities against
  the service accounts this bring-up created, set their ids in the stack file, and dispatch deploy.yml again
  (docs/runbooks/claude-api-wif.md).
- The site grants `pulumi stack output sandbox_job_sa_email` read on the datasets the ACL names as Projects.
- A second Owner who runs Pulumi locally needs encrypt and decrypt on the stack's KMS key
  (docs/runbooks/fresh-environment.md, Local-operator KMS access).
"""


def _say(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="A fresh environment's first bring-up, run once by a project Owner.")
    parser.add_argument('--project', required=True, help='the GCP project of the environment to bring up')
    parser.add_argument(
        '--acl-wait-minutes',
        type=int,
        default=30,
        help="how long to wait for the site's first ACL version before stopping (default: 30)",
    )
    args = parser.parse_args(argv)
    try:
        BringUp(environment(args.project), Shell(), acl_wait=datetime.timedelta(minutes=args.acl_wait_minutes)).run()
    except BringUpError as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
