"""The entry point the worker launches for one file-tool call, in a guest of its own.

Guest-side: shipped into the guest rootfs as ``themis/agent/file_tool.py`` and run by path under ``python3 -I``, so
neither the working directory nor this file's directory is on the import path. It runs one of the SDK's own file tools
— read, write, edit, glob or grep — as the guest's uid, against ``/workspace`` (sandbox-worker.md §"Every tool that
touches the workspace runs in the guest").

The only argument is the path of a request file, a JSON object ``{"tool": <name>, "input": <the tool's input>}``. The
result is one JSON object on stdout: ``{"content": ...}`` holding what the tool returned, a string or a list of content
blocks, or ``{"error": ...}`` holding what the session runner would have sent back with ``is_error`` set had the tool
raised in the worker — a ``ToolError``'s own content, or the ``repr`` of any other exception. A request this program
cannot serve, which only a broken worker sends, exits non-zero with the reason on stderr.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from collections.abc import Sequence

from anthropic.lib import tools
from anthropic.lib.tools import agent_toolset

FILE_TOOLS = frozenset({'read', 'write', 'edit', 'glob', 'grep'})
# Where postern binds the workspace in every guest.
_WORKSPACE = '/workspace'


def _request(path: pathlib.Path) -> tuple[str, dict[str, object]]:
    """The tool name and input a request file holds.

    Raises:
        SystemExit: If the file is not a request for one of the file tools.
    """
    request = json.loads(path.read_text('utf-8'))
    if not isinstance(request, dict) or set(request) != {'tool', 'input'}:
        raise SystemExit(f'{path}: a request is an object with exactly "tool" and "input"')
    name, tool_input = request['tool'], request['input']
    if name not in FILE_TOOLS:
        raise SystemExit(f'{path}: {name!r} is not one of the file tools {sorted(FILE_TOOLS)}')
    if not isinstance(tool_input, dict):
        raise SystemExit(f'{path}: the input to {name} is not an object')
    return name, tool_input


def run(request_path: pathlib.Path, workdir: str) -> dict[str, object]:
    """Serve the request at ``request_path`` against ``workdir``, as the reply this program prints.

    Raises:
        SystemExit: If the file is not a request for one of the file tools.
    """
    name, tool_input = _request(request_path)
    ctx = agent_toolset.AgentToolContext(workdir=workdir)
    (tool,) = (t for t in agent_toolset.beta_agent_toolset_20260401(ctx) if t.name == name)
    try:
        content = asyncio.run(tool.call(tool_input))
    except tools.ToolError as exc:
        return {'error': exc.content if isinstance(exc.content, str) else list(exc.content)}
    except Exception as exc:  # noqa: BLE001 — the reply carries it, as the session runner would have
        return {'error': repr(exc)}
    return {'content': content if isinstance(content, str) else list(content)}


def main(argv: Sequence[str]) -> None:
    """Serve the request named by ``argv``'s one argument and print the reply.

    Raises:
        SystemExit: If ``argv`` names no single request, or the file is not a request for one of the file tools.
    """
    if len(argv) != 1:
        raise SystemExit('usage: file_tool.py REQUEST')
    json.dump(run(pathlib.Path(argv[0]), _WORKSPACE), sys.stdout)


if __name__ == '__main__':
    main(sys.argv[1:])
