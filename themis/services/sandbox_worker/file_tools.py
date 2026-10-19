"""The SDK's file tools, each call run in a guest launch of its own.

See sandbox-worker.md §"Every tool that touches the workspace runs in the guest". `GuestFileTool` presents one of the
SDK's file tools to the session, its name and schema unchanged, and hands every call to a `GuestRunner`. The runner
writes the call to a request file in a host directory every launch binds read-only, launches the entry point the
guest rootfs ships (`guest/file_tool.py`) on it, and turns the reply back into what the SDK's tool would have returned
or raised in the worker.

The skills the SDK downloads stay the worker's; `open_to_reading` lets the guest read them.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import pathlib
import stat
import tempfile
import uuid
from collections.abc import Sequence
from typing import Protocol, override

import postern
from anthropic.lib import tools
from anthropic.types import beta
from anyio import to_thread

# Where every launch finds the request directory; postern binds nothing else under /run/themis.
GUEST_REQUESTS = '/run/themis/file-tool-requests'
# The largest reply is a `read` of a PDF at the SDK's 20 MiB cap, base64-encoded into JSON.
MAX_REPLY_BYTES = 32 * 1024 * 1024


class Launcher(Protocol):
    """What runs a program in a fresh guest: `postern.Sandbox` in the worker."""

    def run(self, argv: list[str], *, timeout: float = 60, max_output: int | None = None) -> postern.ProcResult: ...


def request_directory(host_identity: tuple[int, int] | None) -> pathlib.Path:
    """A private host directory for request files, traversable by whoever bwrap runs as.

    bwrap opens the directory to bind it, and the guest opens a request in it by name, so under a host uid mapping
    the mapped uid has to get through. It need not list it, and never writes it.
    """
    path = pathlib.Path(tempfile.mkdtemp(prefix='themis-file-tool-'))
    if host_identity is not None:
        path.chmod(0o711)
    return path


def _reply(name: str, result: postern.ProcResult, timeout: float) -> tools.BetaFunctionToolResultType:
    """What the SDK's tool ``name`` returned in the guest, per the entry point's ``result``.

    Raises:
        tools.ToolError: Carrying what the session runner would have sent back for the tool raising in the worker.
        RuntimeError: If the launch itself failed: it timed out, overflowed, exited non-zero or printed no reply.
    """
    if result.timed_out:
        raise RuntimeError(f'{name}: the guest was killed after {timeout:g} s')
    if result.truncated:
        raise RuntimeError(f'{name}: the guest replied with more than {MAX_REPLY_BYTES} bytes')
    if result.returncode != 0:
        raise RuntimeError(f'{name}: the guest exited {result.returncode}: {result.stderr.strip()}')
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'{name}: the guest printed no reply: {result.stdout[:200]!r}') from exc
    if not isinstance(reply, dict) or len(reply) != 1 or not {'content', 'error'} & set(reply):
        raise RuntimeError(f'{name}: the guest printed a malformed reply: {result.stdout[:200]!r}')
    if 'error' in reply:
        raise tools.ToolError(_checked(name, reply['error']))
    return _checked(name, reply['content'])


def _checked(name: str, content: object) -> tools.BetaFunctionToolResultType:
    """``content`` as the SDK's file tools produce it: a string, or text blocks and base64 images and documents.

    Anything else came from a guest that is not running the SDK's tools, and is refused rather than passed on: the
    session runner would fail on a malformed block outside its error handling, and an image or document by URL would
    have Anthropic fetch an address the guest chose.

    Raises:
        RuntimeError: If ``content`` is not of that shape.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(_is_sdk_block(block) for block in content):
        return content
    raise RuntimeError(f'{name}: the guest replied with content the SDK tools do not produce: {str(content)[:200]!r}')


def _is_sdk_block(block: object) -> bool:
    if not isinstance(block, dict):
        return False
    kind = block.get('type')
    if kind == 'text':
        return isinstance(block.get('text'), str)
    source = block.get('source')
    return kind in ('image', 'document') and isinstance(source, dict) and source.get('type') == 'base64'


@dataclasses.dataclass(frozen=True)
class GuestRunner:
    """Runs one file-tool call in a fresh guest.

    Attributes:
        launcher: Launches each call; its profile binds ``requests`` read-only at `GUEST_REQUESTS`.
        requests: The host directory request files are written to.
        argv: The command that runs the entry point in the guest; the request's guest path is appended to it.
        timeout: The deadline for one launch, under the SDK's per-tool deadline.
    """

    launcher: Launcher
    requests: pathlib.Path
    argv: Sequence[str]
    timeout: float

    async def call(self, name: str, tool_input: object) -> tools.BetaFunctionToolResultType:
        """Run the SDK's tool ``name`` on ``tool_input`` in a guest, and return or raise what it did.

        Raises:
            tools.ToolError: As the SDK's tool raises it, or carrying the ``repr`` of anything else it raised.
            RuntimeError: If the launch itself failed.
        """
        filename = f'{uuid.uuid4().hex}.json'
        host_path = self.requests / filename
        fd = os.open(host_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        argv = [*self.argv, f'{GUEST_REQUESTS}/{filename}']
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as request:
                os.fchmod(request.fileno(), 0o644)  # past the umask: the guest's uid reads it
                json.dump({'tool': name, 'input': tool_input}, request)
            result = await to_thread.run_sync(
                functools.partial(self.launcher.run, argv, timeout=self.timeout, max_output=MAX_REPLY_BYTES)
            )
        finally:
            host_path.unlink()
        return _reply(name, result, self.timeout)


class GuestFileTool(tools.BetaAsyncBuiltinFunctionTool):
    """One of the SDK's file tools as the session sees it, with every call run in a guest."""

    def __init__(self, sdk_tool: tools.BetaAsyncFunctionTool, runner: GuestRunner) -> None:
        """Stand in for ``sdk_tool``, whose name and schema the session is shown."""
        self._sdk_tool = sdk_tool
        self._runner = runner

    @override
    def to_dict(self) -> beta.BetaToolUnionParam:
        return self._sdk_tool.to_dict()

    @override
    async def call(self, input: object) -> tools.BetaFunctionToolResultType:
        return await self._runner.call(self.name, input)


def open_to_reading(tree: pathlib.Path) -> None:
    """Let any uid read ``tree`` and traverse its directories, leaving owner and write bits alone.

    The SDK downloads skills owner-only, 0700, so under the mapping the guest could not read them. Symlinks are
    skipped, never followed. A missing ``tree`` is left missing.

    Raises:
        ValueError: If ``tree`` is not a directory: a symlink there would have root open whatever it names.
    """
    try:
        top = os.lstat(tree)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(top.st_mode):
        raise ValueError(f'{tree} is not a directory')
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in [*dirnames, *filenames]:
            path = pathlib.Path(dirpath, name)
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(path, stat.S_IMODE(st.st_mode) | 0o555)  # noqa: S103 — read and traverse for the guest, by design
            elif stat.S_ISREG(st.st_mode):
                os.chmod(path, stat.S_IMODE(st.st_mode) | 0o444)
    os.chmod(tree, stat.S_IMODE(top.st_mode) | 0o555)  # noqa: S103 — as above
