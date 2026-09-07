"""The worker's only routes to a `git` binary are the guest's and the mirror's.

That is `Sandbox.run`, whose argv runs in the guest, and the mirror's `BareRepo.git` plus the `git upload-pack` /
`receive-pack` a hatch verdict names. The guest owns `/workspace`, `.git` included, so a `git` the trusted worker
ran there would execute whatever `core.hooksPath` or a clean filter pointed at, in the process holding the
credential (sheaf-changeover.md). The structural guard is the host uid mapping, which the deploy may or may not
enable; this scan is the tripwire that holds regardless — for the shapes a reviewer would otherwise have to spot,
not for code written to evade it.
"""

from __future__ import annotations

import ast
import pathlib
from typing import TypeGuard

import pytest

_PACKAGE = pathlib.Path(__file__).resolve().parents[1]
_SOURCES = sorted(path for path in _PACKAGE.rglob('*.py') if 'tests' not in path.relative_to(_PACKAGE).parts)
# Every way the stdlib spawns a process: none is the worker's to call, so a `git` can reach neither the working
# tree nor anything else from here except through postern (the guest) and `themis.sheaf.wire.bare` (the mirror).
_SPAWNERS = (
    'subprocess',
    'os.system',
    'os.popen',
    'os.exec',
    'os.spawn',
    'os.posix_spawn',
    'os.fork',
    'pty.',
    'multiprocessing',
    'asyncio.create_subprocess',
    'importlib.import_module',
    '__import__',
)
# An event loop's own spawners, reached through whatever name the loop is bound to.
_SPAWNER_METHODS = ('subprocess_exec', 'subprocess_shell')
# A `'git'` literal at the head of an argv is allowed only as an argument of one of these calls: `run` on the
# sandbox, whose argv runs in the guest, and postern's `Process`, whose argv is the mirror's service.
_GIT_ROUTES = {'run', 'Process'}


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name to the dotted name it was imported as: `import os as o` binds `o` to `os`."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({alias.asname or alias.name: alias.name for alias in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            aliases.update({alias.asname or alias.name: f'{node.module}.{alias.name}' for alias in node.names})
    return aliases


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """A callee as its imported dotted name, so `o.system` under `import os as o` reads `os.system`."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value, aliases)
        return f'{prefix}.{node.attr}' if prefix else None
    return None


def _is_spawner(callee: str) -> bool:
    return any(callee == spawner or callee.startswith(spawner) for spawner in _SPAWNERS) or (
        callee.rsplit('.', 1)[-1] in _SPAWNER_METHODS
    )


def _spawns(tree: ast.AST) -> list[str]:
    """Every call whose callee names a process spawner, as `module.attr at line N`."""
    aliases = _aliases(tree)
    return [
        f'{callee} at line {node.lineno}'
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (callee := _dotted(node.func, aliases)) is not None and _is_spawner(callee)
    ]


def _imported_names(tree: ast.AST) -> set[str]:
    """Every dotted name an import brings in: `import x`, and `x.y` for `from x import y`.

    So `from subprocess import run` counts as `subprocess.run` and `from os import system` as
    `os.system`, whatever the local name.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.update(f'{node.module}.{alias.name}' for alias in node.names)
    return names


def _is_git_argv(node: ast.AST) -> TypeGuard[ast.List]:
    return (
        isinstance(node, ast.List)
        and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == 'git'
    )


def _unrouted_git_argvs(tree: ast.AST) -> list[int]:
    """Line numbers of every git argv that is not the direct argument of an allowed route.

    A `['git', ...]` list anywhere but as a route's argument, and a bare `'git'` as any call's first
    positional argument — the unpacked-argv shape `asyncio.create_subprocess_exec('git', ...)` takes.
    """
    aliases = _aliases(tree)
    routed: set[int] = set()
    unpacked: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'git':
            unpacked.append(node.lineno)
        callee = _dotted(node.func, aliases)
        if callee is None or _is_spawner(callee) or callee.rsplit('.', 1)[-1] not in _GIT_ROUTES:
            continue
        routed.update(id(argument) for argument in node.args if _is_git_argv(argument))
    listed = [node.lineno for node in ast.walk(tree) if _is_git_argv(node) and id(node) not in routed]
    return sorted(set(listed + unpacked))


def test_the_scan_covers_the_package() -> None:
    assert {path.name for path in _SOURCES} >= {'worker.py', 'guest_git.py', 'git_hatches.py', 'sync.py'}


@pytest.mark.parametrize('source', _SOURCES, ids=lambda path: path.relative_to(_PACKAGE).as_posix())
def test_the_worker_spawns_no_process_of_its_own(source: pathlib.Path) -> None:
    tree = ast.parse(source.read_text('utf-8'))
    assert _spawns(tree) == [], f'{source.name} spawns a process outside postern and the mirror'
    spawning = sorted(name for name in _imported_names(tree) if _is_spawner(name))
    assert not spawning, f'{source.name} imports a process spawner: {spawning}'


@pytest.mark.parametrize('source', _SOURCES, ids=lambda path: path.relative_to(_PACKAGE).as_posix())
def test_every_git_argv_is_the_guests_or_the_mirrors(source: pathlib.Path) -> None:
    tree = ast.parse(source.read_text('utf-8'))
    unrouted = _unrouted_git_argvs(tree)
    assert unrouted == [], f'{source.name}:{unrouted} builds a git argv outside Sandbox.run and Process'


def test_the_guard_recognises_a_host_side_git() -> None:
    """The scan has to catch the shapes it exists for, or a green run proves nothing."""
    spawned = ast.parse("import subprocess\nsubprocess.run(['git', 'status'], cwd='/workspace')\n")
    assert _spawns(spawned) == ['subprocess.run at line 2']
    assert _unrouted_git_argvs(spawned) == [2]
    assert _unrouted_git_argvs(ast.parse("argv = ['git', 'status']\nsandbox.run(argv)\n")) == [1]
    assert _unrouted_git_argvs(ast.parse("os.execvp('git', ['git', 'status'])\n")) == [1]
    unpacked = ast.parse("await asyncio.create_subprocess_exec('git', 'status', cwd='/workspace')\n")
    assert _spawns(unpacked) == ['asyncio.create_subprocess_exec at line 1']
    assert _unrouted_git_argvs(unpacked) == [1]
    assert _spawns(ast.parse("loop.subprocess_exec(proto, 'git')\n")) == ['loop.subprocess_exec at line 1']
    # `run` is a route name, so an imported symbol has to be caught at the import, whatever it is called locally.
    for source in (
        "from subprocess import run\nrun(['git', 'status'], cwd='/workspace')\n",
        'from os import system as sh\nsh(cmd)\n',
        'from pty import spawn\n',
        'from asyncio import create_subprocess_exec\n',
    ):
        assert any(_is_spawner(name) for name in _imported_names(ast.parse(source))), source
    # An alias resolves back to the module it names, at the call and in the argv check alike.
    aliased = ast.parse("import os as o\no.system(cmd)\nfrom subprocess import run as r\nr(['git', 'x'])\n")
    assert _spawns(aliased) == ['os.system at line 2', 'subprocess.run at line 4']
    assert _unrouted_git_argvs(aliased) == [4]
    assert (
        _unrouted_git_argvs(ast.parse("sandbox.run(['git', 'status'])\nstream.Process(['git', 'upload-pack'])\n")) == []
    )
