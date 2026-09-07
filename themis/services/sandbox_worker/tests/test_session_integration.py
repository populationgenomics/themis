"""On-platform sandbox integration (Linux + bubblewrap only; the `bubblewrap` fixture gates it).

Exercises the isolation properties that only a real bwrap launch can prove: the boot gate passes here, the guest has
no network, and the hatch UDS is the one channel bound in. The typed hatch round-trip and its ``PERMISSION_DENIED`` on
a non-allowlisted method are postern's own e2e (``tests/test_hatch_e2e.py``); the store forwarder's token injection is
``test_hatch.py``. This test's unique value is that all of it holds together on the target platform.
"""

from __future__ import annotations

import pathlib

import postern
import pytest
from postern import grpc as postern_grpc

from themis.services.sandbox_worker import worker

pytestmark = [pytest.mark.sandbox, pytest.mark.usefixtures('bubblewrap')]


def test_verify_passes_on_this_platform() -> None:
    # The worker's boot gate: a successful launch proves userns/netns/seccomp are enforced here.
    postern.Sandbox(postern.SandboxProfile()).verify()


def test_guest_has_no_network() -> None:
    code = (
        'import socket\n'
        'try:\n'
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('REACHED')\n"
        'except OSError as e:\n'
        "    print('NO_NETWORK')\n"
    )
    result = postern.Sandbox(postern.SandboxProfile()).run_python(code, timeout=30)
    assert result.ok, result.stderr
    assert 'NO_NETWORK' in result.stdout
    assert 'REACHED' not in result.stdout


def test_hatch_socket_is_bound_in_and_connectable() -> None:
    hatch = postern_grpc.GrpcHatch(allowlist={'/x.Y/Z'})
    code = (
        'import os, socket\n'
        's = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
        "s.connect(os.environ['POSTERN_HATCH'])\n"  # the one channel out; connecting proves it is bound in
        "print('HATCH_OK')\n"
    )
    try:
        result = postern.Sandbox(postern.SandboxProfile(), hatch=hatch).run_python(code, timeout=30)
    finally:
        hatch.close()
    assert result.ok, result.stderr
    assert 'HATCH_OK' in result.stdout


def test_a_gitconfig_bound_at_etc_reaches_the_guests_git(tmp_path: pathlib.Path) -> None:
    # postern binds none of the rootfs's /etc, so the worker's profile binds the system gitconfig on its own; the
    # bind is `--ro-bind-try`, which would leave the guest's git silently unconfigured were the source path wrong.
    gitconfig = tmp_path / 'gitconfig'
    gitconfig.write_text('[protocol "ext"]\n\tallow = always\n', 'utf-8')
    profile = postern.SandboxProfile(ro_binds=[(str(gitconfig), worker._GUEST_GITCONFIG)])

    result = postern.Sandbox(profile).run(['git', 'config', '--system', '--get', 'protocol.ext.allow'], timeout=30)

    assert result.ok, result.stderr
    assert result.stdout.strip() == 'always'
