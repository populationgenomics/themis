"""The tools that fork `git` beside live gRPC threads turn gRPC's fork logging off, unless the caller chose.

Each case imports the tool in a fresh interpreter, because gRPC reads the setting once, at its own
import, and this test process imported it long ago.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TOOLS = ['tools.clone_analysis', 'tools.sheaf_remote']
# Import the tool before or after gRPC, get gRPC threads running with a channel that tries to connect,
# then fork beside them. `os.fork`, not `subprocess`: on Linux `subprocess` spawns with vfork, which
# skips the atfork handlers gRPC logs from.
_FORK_PROBE = """
import importlib, os, sys
module, order = sys.argv[1:]
if order == 'tool-first':
    importlib.import_module(module)
import grpc
if order == 'grpc-first':
    importlib.import_module(module)
channel = grpc.insecure_channel('127.0.0.1:1')
try:
    grpc.channel_ready_future(channel).result(timeout=0.3)
except grpc.FutureTimeoutError:
    pass
for _ in range(3):
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
print(os.environ['GRPC_ENABLE_FORK_SUPPORT'])
"""
# Import the tool and report the setting. How gRPC reacts to a caller's value differs by platform: on
# Linux, with fork support on, it pauses its threads around a fork and logs nothing.
_SETTING_PROBE = """
import importlib, os, sys
importlib.import_module(sys.argv[1])
print(os.environ['GRPC_ENABLE_FORK_SUPPORT'])
"""


def _probe(probe: str, *args: str, fork_support: str | None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != 'GRPC_ENABLE_FORK_SUPPORT'}
    env['GRPC_VERBOSITY'] = 'INFO'
    if fork_support is not None:
        env['GRPC_ENABLE_FORK_SUPPORT'] = fork_support
    return subprocess.run(  # noqa: S603 — this interpreter, a fixed probe, a module name from the list above
        [sys.executable, '-c', probe, *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


@pytest.mark.parametrize('module', _TOOLS)
def test_importing_the_tool_turns_fork_support_off_before_grpc_reads_it(module: str) -> None:
    result = _probe(_FORK_PROBE, module, 'tool-first', fork_support=None)
    assert result.stdout.strip() == '0'
    assert 'fork_posix' not in result.stderr, 'gRPC read the setting after the tool set it'


@pytest.mark.parametrize('module', _TOOLS)
def test_the_probe_sees_fork_logging_when_grpc_reads_the_setting_first(module: str) -> None:
    result = _probe(_FORK_PROBE, module, 'grpc-first', fork_support=None)
    assert result.stdout.strip() == '0'
    assert 'fork_posix' in result.stderr, 'the probe cannot see fork logging, so the test above proves nothing'


@pytest.mark.parametrize('module', _TOOLS)
def test_a_callers_fork_support_setting_is_kept(module: str) -> None:
    result = _probe(_SETTING_PROBE, module, fork_support='1')
    assert result.stdout.strip() == '1'
