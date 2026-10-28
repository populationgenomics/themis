"""The auth service starts with the ACL settings it is given, and every caller dials it at the audience it checks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import pytest

from themis.services.auth import acl_holder
from themis.services.auth import backend as auth_backend
from themis_infra import auth, capture, deploy_iam

_FLOOR = auth.ACL_REFRESH_INTERVAL_SECONDS + auth.ACL_READ_TIMEOUT_SECONDS
_SECRET_BINDING = 'gcp:secretmanager/secretIamMember:SecretIamMember'  # noqa: S105 — a type token
_PROJECT_BINDING = 'gcp:projects/iAMMember:IAMMember'
# The roles that carry `secretmanager.versions.access`, per https://docs.cloud.google.com/secret-manager/docs/access-control:
# the two Secret Manager roles that list it, and `roles/owner`; `roles/editor` and `roles/viewer` do not carry it.
_PAYLOAD_READING_ROLES = frozenset({'roles/owner', 'roles/secretmanager.admin', 'roles/secretmanager.secretAccessor'})


def test_the_program_knows_the_services_refresh_timing() -> None:
    # The program validates the staleness limit against its own copies: the Pulumi runtime cannot import the service.
    assert auth.ACL_REFRESH_INTERVAL_SECONDS == acl_holder.REFRESH_INTERVAL_SECONDS
    assert auth.ACL_READ_TIMEOUT_SECONDS == auth_backend.ACL_READ_TIMEOUT_SECONDS


def test_the_alert_matches_the_prefix_of_every_rejection_the_service_logs() -> None:
    assert auth.ACL_REJECTED == acl_holder.REJECTED


def test_the_alert_matches_the_line_the_service_logs_on_a_rejection(caplog: pytest.LogCaptureFixture) -> None:
    async def unreadable() -> bytes:
        raise OSError('secret unreachable')

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        asyncio.run(
            acl_holder.AclHolder.load(
                unreadable, read_timeout_seconds=1, staleness_limit_seconds=_FLOOR + 1, clock=lambda: 0.0
            )
        )
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith(auth.ACL_REJECTED)]


def test_a_staleness_limit_within_one_refresh_and_read_is_refused() -> None:
    with pytest.raises(ValueError, match='read timeout'):
        auth.check_acl_staleness_limit(_FLOOR)


def test_a_staleness_limit_past_one_refresh_and_read_is_accepted() -> None:
    auth.check_acl_staleness_limit(_FLOOR + 1)


@pytest.mark.parametrize(
    'writer',
    [
        pytest.param('serviceAccount:pulumi@cpg-common.iam.gserviceaccount.com', id='an-iam-member-string'),
        pytest.param('group:acl-writers@example.org', id='a-group'),
        pytest.param('jane.roe@example.com', id='a-person'),
        pytest.param('pulumi@gserviceaccount.com', id='no-project'),
        pytest.param(' pulumi@cpg-common.iam.gserviceaccount.com', id='whitespace'),
    ],
)
def test_an_acl_writer_that_is_not_a_service_account_is_refused(writer: str) -> None:
    with pytest.raises(ValueError, match="service account's email"):
        auth.check_acl_writer(writer)


def test_the_dev_stacks_acl_settings_are_accepted() -> None:
    config = capture.dev_stack_config()
    auth.check_acl_writer(config['themis:aclWriter'])
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


def _acl_readers(bindings: Iterable[capture.Binding], project: str) -> set[str]:
    """Every member that can read the ACL's payload: by a grant on the secret, or by a project-wide role."""
    return {
        b.member
        for b in bindings
        if b.role in _PAYLOAD_READING_ROLES
        and (
            (b.type_ == _SECRET_BINDING and b.target == auth.ACL_SECRET_ID)
            or (b.type_ == _PROJECT_BINDING and b.target == project)
        )
    }


def _expected_acl_readers(program: capture.Capture) -> set[str]:
    """The auth service, the site's writer, and the deploy account, which administers every secret in the stack."""
    return {
        f'serviceAccount:themis-auth@{program.project}.iam.gserviceaccount.com',
        f'serviceAccount:{capture.dev_stack_config()["themis:aclWriter"]}',
        f'serviceAccount:{deploy_iam.deploy_sa_email(program.project)}',
    }


def test_only_the_auth_service_the_sites_writer_and_the_deploy_account_read_the_acl(program: capture.Capture) -> None:
    assert _acl_readers(program.bindings, program.project) == _expected_acl_readers(program)


def test_a_project_wide_payload_reading_role_for_another_account_is_caught(program: capture.Capture) -> None:
    intruder = capture.Binding(
        urn='urn:pulumi:dev::themis::gcp:projects/iAMMember:IAMMember::intruder',
        type_=_PROJECT_BINDING,
        capability=None,
        member='serviceAccount:themis-web@cpg-themis-dev.iam.gserviceaccount.com',
        role='roles/owner',
        target=program.project,
    )
    assert _acl_readers([*program.bindings, intruder], program.project) != _expected_acl_readers(program)


def test_the_sites_writer_holds_nothing_beyond_the_secret_but_quota(program: capture.Capture) -> None:
    writer = f'serviceAccount:{capture.dev_stack_config()["themis:aclWriter"]}'
    held = {(b.capability, b.target) for b in program.bindings if b.member == writer}
    assert held == {
        ('SecretReader', auth.ACL_SECRET_ID),
        ('SecretVersionManager', auth.ACL_SECRET_ID),
        ('QuotaConsumer', program.project),
    }
