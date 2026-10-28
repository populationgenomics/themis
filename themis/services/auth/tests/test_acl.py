"""The ACL's validation, and the evaluator both rpcs answer from."""

from __future__ import annotations

import json

import pytest

from themis.clients.auth import rules
from themis.rpc import auth_pb2
from themis.services.auth import acl
from themis.services.auth.tests import harness
from themis.testing import auth as auth_fixture

_MANAGER = auth_pb2.ROLE_MANAGER
_CURATOR = auth_pb2.ROLE_CURATOR


def _grant(pid: str, *roles: auth_pb2.Role) -> auth_pb2.ProjectGrant:
    return auth_pb2.ProjectGrant(id=pid, roles=roles)


def _snapshot(document: object = None) -> acl.Snapshot:
    return acl.parse(harness.acl_version(document))


def _with_projects(**projects: object) -> acl.Snapshot:
    return _snapshot({**harness.ACL, 'projects': projects})


def _access(email: str, snapshot: acl.Snapshot | None = None) -> auth_pb2.GetAccessResponse:
    return (_snapshot() if snapshot is None else snapshot).access(email, operator=harness.OPERATOR)


def test_a_person_is_granted_each_project_they_hold_a_role_on_with_those_roles() -> None:
    assert _access(harness.JOHN) == auth_pb2.GetAccessResponse(projects=[_grant('asd', _MANAGER)])
    assert _access(harness.JANE) == auth_pb2.GetAccessResponse(
        projects=[_grant('asd', _CURATOR), _grant('epi', _CURATOR)]
    )


def test_a_person_on_both_lists_holds_both_roles() -> None:
    snapshot = _with_projects(asd={'managers': [harness.JOHN], 'curators': [harness.JOHN]})
    assert _access(harness.JOHN, snapshot) == auth_pb2.GetAccessResponse(projects=[_grant('asd', _MANAGER, _CURATOR)])


def test_a_person_on_no_project_is_not_admitted() -> None:
    assert _access('mallory@example.net') == auth_pb2.GetAccessResponse()


def test_an_email_matches_whatever_its_case() -> None:
    assert _access(harness.JOHN.upper()) == _access(harness.JOHN)


def test_a_non_ascii_character_never_folds_into_a_member_s_ascii_address() -> None:
    # U+212A KELVIN SIGN lowercases to 'k' under Unicode rules.
    snapshot = _with_projects(asd={'managers': ['kate@example.com'], 'curators': []})
    assert not _access('\u212aate@example.com', snapshot).projects


def test_the_operator_account_holds_both_roles_on_every_project_though_the_acl_never_lists_it() -> None:
    assert not any(harness.OPERATOR in project.managers | project.curators for project in _snapshot().projects.values())
    assert _access(harness.OPERATOR) == auth_pb2.GetAccessResponse(
        projects=[_grant('asd', _MANAGER, _CURATOR), _grant('epi', _MANAGER, _CURATOR)]
    )


def test_the_operator_account_matches_whatever_its_case() -> None:
    assert _access(harness.OPERATOR.upper()) == _access(harness.OPERATOR)


def test_the_operator_account_reaches_nothing_in_an_acl_holding_no_project() -> None:
    assert _access(harness.OPERATOR, _with_projects()) == auth_pb2.GetAccessResponse()


def test_another_account_of_the_deployment_reaches_only_what_the_acl_lists() -> None:
    web = rules.account_email('themis-web', auth_fixture.PROJECT)
    assert _access(web) == auth_pb2.GetAccessResponse()
    listed = _with_projects(epi={'managers': [], 'curators': [web]})
    assert _access(web, listed) == auth_pb2.GetAccessResponse(projects=[_grant('epi', _CURATOR)])


def test_the_operator_account_of_another_project_is_not_the_operator() -> None:
    assert _access(rules.operator_email('another-project')) == auth_pb2.GetAccessResponse()


def test_a_project_s_session_grant_carries_no_role_and_an_absent_one_has_none() -> None:
    snapshot = _snapshot()
    assert snapshot.grant('epi') == _grant('epi')
    assert snapshot.grant('gone') is None


def test_a_grant_handed_out_cannot_change_the_snapshot() -> None:
    snapshot = _snapshot()
    granted = _access(harness.JOHN, snapshot).projects[0]
    granted.id = 'changed'
    granted.roles.append(_CURATOR)
    assert _access(harness.JOHN, snapshot) == auth_pb2.GetAccessResponse(projects=[_grant('asd', _MANAGER)])


def _broken(path: tuple[str | int, ...], value: object) -> bytes:
    """The fixture's ACL with the value at ``path`` replaced, or removed where ``value`` is ``_REMOVE``."""
    document = json.loads(harness.acl_version())
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    if value is _REMOVE:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    return harness.acl_version(document)


_REMOVE = object()
_ASD_PATH = ('projects', 'asd')
_MANAGERS = (*_ASD_PATH, 'managers')
_JOHN_PATH = (*_MANAGERS, 0)


@pytest.mark.parametrize(
    ('raw', 'reason'),
    [
        pytest.param(b'\xff', 'not UTF-8', id='not-utf8'),
        pytest.param(b'{', 'not JSON', id='not-json'),
        pytest.param(b'[]', 'not an object', id='not-an-object'),
        pytest.param(b'[' * 100_000, 'not JSON', id='nested-past-the-recursion-limit'),
        pytest.param(b'{"format": 1' + b'0' * 5000 + b'}', 'not JSON', id='integer-past-the-conversion-limit'),
        pytest.param(_broken(_JOHN_PATH, ' john.doe@example.org'), 'whitespace', id='email-whitespace'),
        pytest.param(_broken(_JOHN_PATH, 'john.doe'), 'not an email', id='email-without-domain'),
        pytest.param(_broken(_JOHN_PATH, 'john doe@example.org'), 'not an email', id='email-inner-space'),
        pytest.param(_broken(_JOHN_PATH, 'john@doe@example.org'), 'not an email', id='email-two-ats'),
        pytest.param(_broken(_JOHN_PATH, 'j\xd6rg@example.org'), 'not lowercase', id='member-non-ascii-upper'),
        pytest.param(_broken(_JOHN_PATH, '\u212aate@example.org'), 'not lowercase', id='member-kelvin-sign'),
        pytest.param(_broken(_JOHN_PATH, 'John.Doe@example.org'), 'not lowercase', id='member-upper'),
        pytest.param(_broken(_JOHN_PATH, 7), 'non-empty string', id='email-not-a-string'),
        pytest.param(_broken((*_ASD_PATH, 'curators', 0), 'Jane@example.com'), 'not lowercase', id='curator-upper'),
        pytest.param(_broken(('format',), 2), 'format', id='unknown-format'),
        pytest.param(_broken(('format',), True), 'format', id='format-not-an-integer'),
        pytest.param(_broken(('format',), _REMOVE), 'lacks', id='format-missing'),
        pytest.param(_broken(('owner',), 'site'), 'unknown key', id='unknown-top-level-key'),
        pytest.param(_broken(('admitted',), [harness.JOHN]), 'unknown key', id='admitted-key'),
        pytest.param(_broken(('projects',), [harness.JOHN]), 'not an object', id='projects-a-list'),
        pytest.param(_broken(_ASD_PATH, [harness.JOHN]), 'not an object', id='project-a-list'),
        pytest.param(_broken(_ASD_PATH, None), 'not an object', id='project-null'),
        pytest.param(_broken((*_ASD_PATH, 'members'), [harness.JOHN]), 'unknown key', id='members-key'),
        pytest.param(_broken((*_ASD_PATH, 'name'), 'ASD'), 'unknown key', id='name-key'),
        pytest.param(_broken(_MANAGERS, _REMOVE), 'lacks', id='managers-missing'),
        pytest.param(_broken((*_ASD_PATH, 'curators'), _REMOVE), 'lacks', id='curators-missing'),
        pytest.param(_broken(_MANAGERS, harness.JOHN), 'not a list', id='managers-a-string'),
        pytest.param(_broken(_MANAGERS, [harness.JOHN, harness.JOHN]), 'repeats', id='managers-repeat'),
        pytest.param(
            _broken(_ASD_PATH, {'managers': [], 'curators': []}), 'no manager and no curator', id='project-empty'
        ),
        pytest.param(
            b'{"format": 1, "projects": {"a": {"managers": ["x@example.org"], "curators": []},'
            b' "a": {"managers": [], "curators": ["x@example.org"]}}}',
            'repeats the key',
            id='duplicate-project-id',
        ),
        pytest.param(
            b'{"format": 1, "projects": {"a": {"managers": ["x@example.org"], "managers": [], "curators": []}}}',
            'repeats the key',
            id='duplicate-role-key',
        ),
    ],
)
def test_a_version_breaking_a_rule_is_rejected_whole_naming_the_rule(raw: bytes, reason: str) -> None:
    with pytest.raises(acl.AclError, match=reason):
        acl.parse(raw)


_SOMEONE = {'managers': [harness.JOHN], 'curators': []}


@pytest.mark.parametrize('pid', ['cohort/2024', '..', 'A', 'a_b', 'a\x00b', '', ' asd', 'asd\n', '-asd', 'caf\xe9'])
def test_a_project_id_that_is_not_a_lowercase_slug_rejects_the_acl(pid: str) -> None:
    with pytest.raises(acl.AclError, match='not a lowercase slug'):
        acl.parse(harness.acl_version({**harness.ACL, 'projects': {pid: _SOMEONE}}))


def test_a_project_id_of_letters_digits_and_hyphens_is_taken() -> None:
    snapshot = _with_projects(**{'acute-care': _SOMEONE, '2024-cohort-b': _SOMEONE})
    assert set(snapshot.projects) == {'acute-care', '2024-cohort-b'}


def test_a_lowercase_non_ascii_address_is_taken_and_matched_exactly() -> None:
    snapshot = _with_projects(asd={'managers': [], 'curators': ['j\xf6rg@example.org']})
    assert _access('j\xf6rg@example.org', snapshot).projects


def test_the_fixture_acl_is_valid() -> None:
    # The rejection cases above each break one rule of this ACL; were it invalid, they would pass vacuously.
    assert _snapshot().projects
