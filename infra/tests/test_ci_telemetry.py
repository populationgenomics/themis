"""The CI telemetry account writes metrics and nothing else, and the workflows that report through it name it.

Any GitHub Actions run of the repository may take the account, so what it holds is what every workflow — a
pull request's edited workflow included — can do in the project. The Claude Code workflows are the ones
that report through it: each must federate into the program's account, and hand Claude Code the token the
federation yields.
"""

from __future__ import annotations

import json
import pathlib

import yaml

from themis_infra import capture, ci_telemetry

_WORKFLOWS = pathlib.Path(__file__).resolve().parents[2] / '.github' / 'workflows'
_CLAUDE_CODE_ACTION = 'anthropics/claude-code-action@'
_AUTH_ACTION = 'google-github-actions/auth@'


def _account_email(program: capture.Capture) -> str:
    return f'{ci_telemetry.ACCOUNT_ID}@{program.project}.iam.gserviceaccount.com'


def test_the_account_holds_only_telemetry_writes(program: capture.Capture) -> None:
    member = f'serviceAccount:{_account_email(program)}'
    held = {binding.capability for binding in program.bindings if binding.member == member}
    assert held == {'TelemetryWriter'}


def test_only_the_repository_may_take_the_account(program: capture.Capture) -> None:
    account = f'projects/{program.project}/serviceAccounts/{_account_email(program)}'
    [(capability, member)] = [(b.capability, b.member) for b in program.bindings if b.target == account]
    assert capability == 'FederatedImpersonator'
    assert member.startswith('principalSet://iam.googleapis.com/projects/')
    assert member.endswith(f'/attribute.repository/{ci_telemetry.REPOSITORY}')


def _steps(workflow: pathlib.Path) -> list[dict[str, object]]:
    jobs = yaml.safe_load(workflow.read_text('utf-8'))['jobs']
    return [step for job in jobs.values() for step in job['steps']]


def _uses(step: dict[str, object], action: str) -> bool:
    return str(step.get('uses', '')).startswith(action)


def _inputs(step: dict[str, object]) -> dict[str, object]:
    inputs = step['with']
    assert isinstance(inputs, dict)
    return inputs


def _claude_code_workflows() -> list[tuple[pathlib.Path, list[dict[str, object]]]]:
    found = [(path, _steps(path)) for path in sorted(_WORKFLOWS.glob('*.yml'))]
    return [(path, steps) for path, steps in found if any(_uses(step, _CLAUDE_CODE_ACTION) for step in steps)]


def test_every_claude_code_workflow_reports_through_the_account(program: capture.Capture) -> None:
    workflows = _claude_code_workflows()
    assert workflows, 'no workflow runs claude-code-action; the glob is not finding them'
    email = _account_email(program)
    for path, steps in workflows:
        [auth] = [s for s in steps if _uses(s, _AUTH_ACTION) and _inputs(s).get('service_account') == email]
        assert _inputs(auth)['token_format'] == 'access_token', path.name  # noqa: S105 — a token's format, not a token
        token = f'${{{{ steps.{auth["id"]}.outputs.access_token }}}}'
        for step in steps:
            if not _uses(step, _CLAUDE_CODE_ACTION):
                continue
            settings = _inputs(step)['settings']
            assert isinstance(settings, str)
            env = json.loads(settings)['env']
            assert env['CLAUDE_CODE_ENABLE_TELEMETRY'] == '1', path.name
            assert env['OTEL_EXPORTER_OTLP_HEADERS'] == f'Authorization=Bearer {token}', path.name
