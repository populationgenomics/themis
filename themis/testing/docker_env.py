"""Point testcontainers at the Docker daemon the ``docker`` CLI is pointed at.

docker-py, the client testcontainers builds on, resolves the daemon from ``DOCKER_HOST`` and
otherwise dials ``/var/run/docker.sock``. It never reads the CLI's contexts, so where a
contributor's daemon answers somewhere else and leaves that path empty — Colima, OrbStack — every
container-backed test fails to connect while ``docker`` itself works, and the suite is usable only
by exporting environment variables by hand. This module derives those variables from the endpoint
actually in use; the repo's root ``conftest.py`` exports the ones the environment leaves empty.

Only a unix socket is derived from. Reaching a daemon over TCP or SSH takes more than these two
variables carry — the TLS material a context stores, an ssh transport — and exporting
``DOCKER_HOST`` takes the CLI off the context too, so the suite's own daemon probe would start
failing along with it, skipping the container tests instead of failing them.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import urllib.parse
from collections.abc import Mapping

# The socket docker-py dials when nothing says otherwise, and the one a daemon serves in its own
# filesystem.
_DEFAULT_SOCKET = '/var/run/docker.sock'

_INSPECT_TIMEOUT_S = 10


def env_additions(environ: Mapping[str, str]) -> dict[str, str]:
    """The variables `environ` lacks to reach the Docker daemon in use.

    Args:
        environ: The environment as it stands, normally ``os.environ``. Its ``DOCKER_HOST`` is the
            endpoint everything is derived from; where it names none, the CLI's active context
            does. Its ``HOME`` decides which sockets count as forwarded out of a VM.

    Returns:
        The variables to add, never one `environ` already gives a non-empty value: empty whenever
        the environment suffices, and whenever there is nothing to derive.
    """
    endpoint = environ.get('DOCKER_HOST') or active_context_endpoint()
    if endpoint is None:
        return {}
    derived = _env_for_endpoint(endpoint, environ.get('HOME', ''))
    return {name: value for name, value in derived.items() if not environ.get(name)}


def active_context_endpoint() -> str | None:
    """The Docker endpoint of the CLI's active context.

    Answers from the local context store rather than the daemon, so it reports an endpoint whether
    or not anything is listening on it.

    Returns:
        The endpoint as the CLI reports it (``unix://…``, ``tcp://…``, ``ssh://…``), or ``None``
        when there is no CLI on ``PATH``, the call fails, or the context names no endpoint —
        cases where a caller has nothing to point anything at and should leave the environment as
        it found it.
    """
    docker = shutil.which('docker')
    if docker is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [docker, 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _env_for_endpoint(endpoint: str, home: str) -> dict[str, str]:
    """The variables that point testcontainers at `endpoint`.

    Args:
        endpoint: A Docker endpoint, as the CLI reports one for a context.
        home: The user's home directory, against which a socket is judged forwarded or not.

    Returns:
        ``DOCKER_HOST`` for a unix socket docker-py would not dial on its own, plus
        ``TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`` where that socket is forwarded out of the VM the
        daemon runs in: testcontainers bind-mounts the client's socket into its reaper container,
        and a mount source has to be a path in the daemon's own filesystem. Empty for every
        endpoint that is not a unix socket, and for the socket docker-py dials unaided.
    """
    socket_path = _unix_socket_path(endpoint)
    if socket_path is None or socket_path == _DEFAULT_SOCKET:
        return {}
    env = {'DOCKER_HOST': endpoint}
    if _is_forwarded_from_a_vm(socket_path, home):
        env['TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE'] = _DEFAULT_SOCKET
    return env


def _unix_socket_path(endpoint: str) -> str | None:
    """The absolute socket path `endpoint` names, or ``None`` where it names none.

    A bare ``unix://`` names none: docker-py reads it as its own default socket.
    """
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != 'unix' or not parsed.path.startswith('/'):
        return None
    return parsed.path


def _is_forwarded_from_a_vm(socket_path: str, home: str) -> bool:
    """Whether a socket at `socket_path` is a forward of one inside the daemon's VM.

    Colima, OrbStack, Rancher Desktop and Docker Desktop all forward their VM's socket to a path
    under the user's home directory. A daemon sharing this filesystem serves the socket itself —
    a rootless one under its runtime directory, a system one under ``/var/run`` — and that path
    is one it can mount.

    Args:
        socket_path: The socket the client dials.
        home: The user's home directory. ``/`` and an empty value tell nothing apart, so every
            socket is then taken to be the daemon's own.
    """
    if home in {'', '/'}:
        return False
    return pathlib.Path(socket_path).is_relative_to(home)
