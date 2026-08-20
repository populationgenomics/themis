"""Which Docker endpoints imply which variables, and which of the environment's own survive.

Both ways this derivation can be wrong are quiet. A socket override the daemon cannot mount fails
only once a test starts a container, and a variable exported where none was wanted changes which
daemon the suite runs against — including in CI, where the derivation has to come out empty.
"""

from __future__ import annotations

import pathlib
import shutil
import urllib.parse
from collections.abc import Callable

import pytest

from themis.testing import docker_env

_HOME = '/Users/dev'
_VM_SOCKET = f'unix://{_HOME}/.colima/default/docker.sock'
_ROOTLESS_SOCKET = 'unix:///run/user/1000/docker.sock'
_OVERRIDE = 'TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE'


def _install_docker(monkeypatch: pytest.MonkeyPatch, directory: pathlib.Path, body: str) -> None:
    stub = directory / 'docker'
    stub.write_text(f'#!/bin/sh\n{body}\n', 'utf-8')
    stub.chmod(0o755)
    monkeypatch.setenv('PATH', str(directory))


@pytest.fixture
def active_context(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Callable[[str], None]:
    """Hands back a call that puts a ``docker`` reporting a given endpoint on ``PATH``.

    The stub answers ``context inspect`` and nothing else, so code that stops asking for the
    context fails here rather than passing on the stub's indifference.
    """

    def report(endpoint: str) -> None:
        _install_docker(monkeypatch, tmp_path, f'test "$1 $2" = "context inspect" || exit 2\necho {endpoint}')

    return report


@pytest.fixture
def broken_docker_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A ``docker`` that fails, as a half-installed or misconfigured one does."""
    _install_docker(monkeypatch, tmp_path, 'echo "context store unreadable" >&2\nexit 1')


@pytest.fixture
def no_docker_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A ``PATH`` with no ``docker`` on it."""
    monkeypatch.setenv('PATH', str(tmp_path))


def test_the_socket_docker_py_dials_needs_no_variables(active_context: Callable[[str], None]) -> None:
    active_context('unix:///var/run/docker.sock')
    assert docker_env.env_additions({'HOME': _HOME}) == {}


def test_a_socket_forwarded_from_a_vm_sends_the_reaper_to_the_daemons_own(
    active_context: Callable[[str], None],
) -> None:
    active_context(_VM_SOCKET)
    assert docker_env.env_additions({'HOME': _HOME}) == {
        'DOCKER_HOST': _VM_SOCKET,
        _OVERRIDE: '/var/run/docker.sock',
    }


def test_a_daemon_sharing_the_filesystem_keeps_the_socket_it_serves(active_context: Callable[[str], None]) -> None:
    active_context(_ROOTLESS_SOCKET)
    assert docker_env.env_additions({'HOME': _HOME}) == {'DOCKER_HOST': _ROOTLESS_SOCKET}


@pytest.mark.parametrize('endpoint', ['tcp://192.168.64.2:2376', 'ssh://dev@build-host', 'unix://'])
def test_a_daemon_on_no_socket_path_is_left_to_the_environment(
    active_context: Callable[[str], None], endpoint: str
) -> None:
    active_context(endpoint)
    assert docker_env.env_additions({'HOME': _HOME}) == {}


@pytest.mark.parametrize('environ', [{'HOME': ''}, {'HOME': '/'}, {}])
def test_a_home_that_tells_nothing_apart_claims_no_socket(
    active_context: Callable[[str], None], environ: dict[str, str]
) -> None:
    active_context(_VM_SOCKET)
    assert docker_env.env_additions(environ) == {'DOCKER_HOST': _VM_SOCKET}


def test_an_endpoint_the_environment_names_gets_only_what_it_lacks(no_docker_cli: None) -> None:
    del no_docker_cli  # the endpoint is the environment's; no CLI is consulted for one
    additions = docker_env.env_additions({'DOCKER_HOST': _VM_SOCKET, 'HOME': _HOME})
    assert additions == {_OVERRIDE: '/var/run/docker.sock'}


def test_nothing_the_environment_already_sets_is_replaced(no_docker_cli: None) -> None:
    del no_docker_cli  # the endpoint is the environment's; no CLI is consulted for one
    environ = {'DOCKER_HOST': _VM_SOCKET, _OVERRIDE: '/run/host-services/docker.sock', 'HOME': _HOME}
    assert docker_env.env_additions(environ) == {}


def test_an_empty_docker_host_falls_through_to_the_active_context(active_context: Callable[[str], None]) -> None:
    active_context(_VM_SOCKET)
    assert docker_env.env_additions({'DOCKER_HOST': '', 'HOME': _HOME}) == {
        'DOCKER_HOST': _VM_SOCKET,
        _OVERRIDE: '/var/run/docker.sock',
    }


def test_a_machine_with_no_docker_cli_gets_nothing(no_docker_cli: None) -> None:
    del no_docker_cli  # the gate under test
    assert docker_env.env_additions({'HOME': _HOME}) == {}


def test_a_docker_cli_that_fails_gets_nothing(broken_docker_cli: None) -> None:
    del broken_docker_cli  # the gate under test
    assert docker_env.env_additions({'HOME': _HOME}) == {}


@pytest.mark.skipif(shutil.which('docker') is None, reason='needs the docker CLI')
def test_the_real_cli_answers_the_query_this_module_makes() -> None:
    """The format template is the one string here that no stub can keep honest."""
    endpoint = docker_env.active_context_endpoint()
    assert endpoint is not None
    assert urllib.parse.urlparse(endpoint).scheme in {'unix', 'tcp', 'ssh', 'npipe'}
