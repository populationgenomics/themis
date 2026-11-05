"""The auth service starts with the ACL settings it is given, and every caller dials it at the audience it checks."""

from __future__ import annotations

import pathlib
from collections.abc import Iterable

import pytest

from themis.services.auth import acl_holder
from themis.services.auth import backend as auth_backend
from themis_infra import auth, capture, deploy_iam

_FLOOR = auth.ACL_REFRESH_INTERVAL_SECONDS + auth.ACL_READ_TIMEOUT_SECONDS
_SECRET_BINDING_PREFIX = 'gcp:secretmanager/'  # noqa: S105 — a type token's prefix
_PROJECT = 'cpg-themis-dev'
_PROJECT_NUMBER = '698160839187'
_PROJECT_BINDING = 'gcp:projects/iAMMember:IAMMember'
# The roles that carry `secretmanager.versions.access`, per https://docs.cloud.google.com/secret-manager/docs/access-control:
# the two Secret Manager roles that list it, and `roles/owner`; `roles/editor` and `roles/viewer` do not carry it.
_PAYLOAD_READING_ROLES = frozenset({'roles/owner', 'roles/secretmanager.admin', 'roles/secretmanager.secretAccessor'})


def test_the_program_knows_the_services_refresh_timing() -> None:
    # The program validates the staleness limit against its own copies: the Pulumi runtime cannot import the service.
    assert auth.ACL_REFRESH_INTERVAL_SECONDS == acl_holder.REFRESH_INTERVAL_SECONDS
    assert auth.ACL_READ_TIMEOUT_SECONDS == auth_backend.ACL_READ_TIMEOUT_SECONDS


def test_a_staleness_limit_within_one_refresh_and_read_is_refused() -> None:
    with pytest.raises(ValueError, match='read timeout'):
        auth.check_acl_staleness_limit(_FLOOR)


def test_a_staleness_limit_past_one_refresh_and_read_is_accepted() -> None:
    auth.check_acl_staleness_limit(_FLOOR + 1)


@pytest.mark.parametrize(
    'name',
    [
        pytest.param('projects/cpg-themis-dev/secrets/acl', id='project-id'),
        pytest.param('projects/698160839187/secrets/acl', id='project-number'),
    ],
)
def test_an_acl_secret_named_by_its_resource_name_is_accepted(name: str) -> None:
    auth.check_acl_secret(name, project=_PROJECT, project_number=_PROJECT_NUMBER)


@pytest.mark.parametrize(
    'name',
    [
        pytest.param('acl', id='bare-id'),
        pytest.param('projects/cpg-themis-dev/secrets/acl/versions/latest', id='a-version'),
        pytest.param('projects/cpg-themis-dev/secrets/', id='no-secret-id'),
        pytest.param('projects/Cpg-Themis-Dev/secrets/acl', id='uppercase-project'),
        pytest.param(' projects/cpg-themis-dev/secrets/acl', id='whitespace'),
        pytest.param('projects/0/secrets/acl', id='project-number-zero'),
    ],
)
def test_an_acl_secret_not_named_by_its_resource_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="secret's resource name"):
        auth.check_acl_secret(name, project=_PROJECT, project_number=_PROJECT_NUMBER)


@pytest.mark.parametrize(
    'name',
    [
        pytest.param('projects/cpg-themis-prod/secrets/acl', id='another-project-id'),
        pytest.param('projects/111111111111/secrets/acl', id='another-project-number'),
    ],
)
def test_an_acl_secret_in_another_project_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="deployment's project"):
        auth.check_acl_secret(name, project=_PROJECT, project_number=_PROJECT_NUMBER)


@pytest.mark.parametrize('stack_file', capture.stack_files(), ids=lambda path: path.name)
def test_every_stacks_acl_settings_are_accepted(stack_file: pathlib.Path) -> None:
    config = capture.stack_config(stack_file.name.removeprefix('Pulumi.').removesuffix('.yaml'))
    # The stack files name the project by id; the number is the program's lookup.
    auth.check_acl_secret(config['themis:aclSecret'], project=config['gcp:project'], project_number='')
    auth.check_acl_staleness_limit(int(config['themis:aclStalenessLimitSeconds']))


def _envs(registration: capture.Registration) -> dict[str, object]:
    template = registration.state['template']
    template = template.get('template', template)  # a job nests its task template one level down
    return {env['name']: env.get('value') for container in template['containers'] for env in container.get('envs', [])}


def test_every_caller_dials_auth_at_the_audience_it_checks(program: capture.Capture) -> None:
    # The auth service's interceptor declines an ID token minted for any URL but its audience, so a caller handed the
    # other run.app URL would be refused on every call.
    workloads = capture.of_type(program, 'gcp:cloudrunv2/service:Service') + capture.of_type(
        program, 'gcp:cloudrunv2/job:Job'
    )
    envs = {str(workload.state['name']): _envs(workload) for workload in workloads}
    audience = envs['themis-auth']['THEMIS_ID_TOKEN_AUDIENCE']
    dialed = {name: env['THEMIS_AUTH_URL'] for name, env in envs.items() if 'THEMIS_AUTH_URL' in env}
    assert dialed, 'no workload dials the auth service; the check is vacuous'
    assert all(url == audience for url in dialed.values()), f'auth checks {audience}, callers dial {dialed}'


def test_the_auth_service_reads_the_configured_acl_secret(program: capture.Capture) -> None:
    (service,) = [
        workload
        for workload in capture.of_type(program, 'gcp:cloudrunv2/service:Service')
        if workload.state['name'] == 'themis-auth'
    ]
    assert _envs(service)['THEMIS_ACL_SECRET'] == capture.stack_config('dev')['themis:aclSecret']


def test_the_stack_grants_nothing_on_the_acl_secret(program: capture.Capture) -> None:
    name = capture.stack_config('dev')['themis:aclSecret']
    on_acl = [
        b.urn
        for b in program.bindings
        if b.type_.startswith(_SECRET_BINDING_PREFIX) and b.target in {name, name.rsplit('/', 1)[1]}
    ]
    assert not on_acl, 'bindings on the ACL secret:\n' + '\n'.join(on_acl)


def _project_wide_secret_readers(bindings: Iterable[capture.Binding], project: str) -> set[str]:
    """Every member that can read any secret's payload in the project, the ACL's included, by a project-wide role."""
    return {
        b.member
        for b in bindings
        if b.type_ == _PROJECT_BINDING and b.target == project and b.role in _PAYLOAD_READING_ROLES
    }


def _deploy_account(program: capture.Capture) -> set[str]:
    return {f'serviceAccount:{deploy_iam.deploy_sa_email(program.project)}'}


def test_only_the_deploy_account_reads_secrets_project_wide(program: capture.Capture) -> None:
    assert _project_wide_secret_readers(program.bindings, program.project) == _deploy_account(program)


def test_a_project_wide_payload_reading_role_for_another_account_is_caught(program: capture.Capture) -> None:
    intruder = capture.Binding(
        urn='urn:pulumi:dev::themis::gcp:projects/iAMMember:IAMMember::intruder',
        type_=_PROJECT_BINDING,
        capability=None,
        member='serviceAccount:themis-web@cpg-themis-dev.iam.gserviceaccount.com',
        role='roles/owner',
        target=program.project,
    )
    assert _project_wide_secret_readers([*program.bindings, intruder], program.project) != _deploy_account(program)
