"""A content check: code the deployment names in the hook's environment, called for each new commit of a push.

The hook knows nothing about what the check reads. It passes the commit and its parents, reports each reason as its
own refusal, and refuses the push outright when the check it was told of cannot be loaded.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from themis import sheaf
from themis.sheaf.tests import conftest, content_checks
from themis.sheaf.wire import protect, server

REPO = 'projects/demo'
CHECK = 'themis.sheaf.tests.content_checks:refuse_a_forbidden_file'


def _server(backend: sheaf.LocalBackend, tmp_path: pathlib.Path, check: str) -> server.SheafGitServer:
    return server.SheafGitServer.over_backend(
        backend, tmp_path / 'bare', repos={REPO}, protection=protect.Protection(content_check=check)
    )


@pytest.fixture
def checked(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> Iterator[server.SheafGitServer]:
    with _server(backend, tmp_path, CHECK) as instance:
        yield instance


def _clone(instance: server.SheafGitServer, tmp_path: pathlib.Path) -> pathlib.Path:
    work = tmp_path / 'work'
    conftest.run_git('clone', instance.url(REPO), str(work), cwd=tmp_path)
    return work


def _commit(work: pathlib.Path, name: str) -> None:
    (work / name).write_text('x\n', 'utf-8')
    conftest.run_git('add', name, cwd=work)
    conftest.run_git('commit', '-qm', f'write {name}', cwd=work)


def test_the_check_is_named_in_the_environment_and_read_back() -> None:
    protection = protect.Protection(paths=('scratch/**',), content_check=CHECK)
    assert protect.Protection.from_env(protection.as_env()) == protection
    assert protection.load_check() is content_checks.refuse_a_forbidden_file
    assert protect.Protection.unprotected().load_check() is None


@pytest.mark.parametrize('name', ['no-colon', 'a:b:c'])
def test_a_check_named_other_than_module_colon_attribute_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match='module:attribute'):
        protect.Protection(content_check=name)


@pytest.mark.parametrize(
    'name',
    [
        'themis.sheaf.tests.nowhere:check',
        'themis.sheaf.tests.content_checks:absent',
        f'{content_checks.__name__}:NOT_CALLABLE',
    ],
)
def test_a_check_that_cannot_be_loaded_is_a_fault_not_a_pass(name: str) -> None:
    with pytest.raises(ValueError, match='content check'):
        protect.Protection(content_check=name).load_check()


def test_a_commit_the_check_refuses_is_refused_with_its_reason(
    checked: server.SheafGitServer, tmp_path: pathlib.Path
) -> None:
    work = _clone(checked, tmp_path)
    _commit(work, 'fine.txt')
    _commit(work, content_checks.FORBIDDEN)

    refused = conftest.run_git('push', 'origin', 'HEAD:refs/heads/main', cwd=work, check=False)

    assert refused.returncode != 0
    assert f'holds {content_checks.FORBIDDEN} (parents: 1)' in refused.stderr
    assert 'change what each refusal names' in refused.stderr


def test_commits_the_check_passes_land(checked: server.SheafGitServer, tmp_path: pathlib.Path) -> None:
    work = _clone(checked, tmp_path)
    _commit(work, 'fine.txt')

    pushed = conftest.run_git('push', 'origin', 'HEAD:refs/heads/main', cwd=work, check=False)

    assert pushed.returncode == 0, pushed.stderr


def test_a_check_the_hook_cannot_load_refuses_every_push(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    with _server(backend, tmp_path, 'themis.sheaf.tests.nowhere:check') as instance:
        work = _clone(instance, tmp_path)
        _commit(work, 'fine.txt')
        refused = conftest.run_git('push', 'origin', 'HEAD:refs/heads/main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'deployment fault' in refused.stderr


def test_a_check_that_raises_refuses_the_push_as_a_fault(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    with _server(backend, tmp_path, 'themis.sheaf.tests.content_checks:fail') as instance:
        work = _clone(instance, tmp_path)
        _commit(work, 'fine.txt')
        refused = conftest.run_git('push', 'origin', 'HEAD:refs/heads/main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'the content check failed' in refused.stderr
    assert 'TimeoutExpired' in refused.stderr
    assert 'deployment fault' in refused.stderr
    assert 'Traceback' not in refused.stderr
