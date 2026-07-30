"""The guest rootfs is closed under the first-party imports of everything it ships.

The Dockerfile's guest stage copies named paths into the guest's site-packages, and that copy is the guest's only
source of first-party code, so a shipped module importing any `themis` module outside the copied set raises at
import time and leaves the hatch unreachable. Nothing about the failure is particular to `themis.rpc`: the guest
also ships hand-authored code and whole directories, and either can grow an import the copy does not satisfy.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_DOCKERFILE = pathlib.Path(__file__).resolve().parents[1] / 'Dockerfile'
_GUEST_STAGE = 'guest'
_SITE_PACKAGES = '/site-packages/'
_COPY = re.compile(r'^[ \t]*(?i:COPY|ADD)[ \t]+(?P<argv>.+)$', re.MULTILINE)
_FROM = re.compile(r'^[ \t]*(?i:FROM)[ \t]+\S+(?:[ \t]+(?i:AS)[ \t]+(?P<stage>\S+))?[ \t]*$', re.MULTILINE)


def _instructions() -> str:
    """The Dockerfile as one line per instruction: comment lines dropped, line continuations folded."""
    body = re.sub(r'^[ \t]*#[^\n]*\n', '', _DOCKERFILE.read_text('utf-8'), flags=re.MULTILINE)
    return re.sub(r'\\[ \t]*\r?\n[ \t]*', ' ', body)


def _guest_stage() -> str:
    """The body of the stage that assembles the rootfs: the instructions between its FROM and the next."""
    instructions = _instructions()
    stages = list(_FROM.finditer(instructions))
    # A FROM the pattern does not match would not end the preceding stage, so the next stage's COPYs would
    # read as guest-bound — extra landings, which satisfy imports the guest does not ship.
    declared = re.findall(r'^[ \t]*(?i:FROM)[ \t]', instructions, re.MULTILINE)
    assert len(stages) == len(declared), 'the Dockerfile has a FROM the stage scan does not parse'
    bodies = [
        instructions[stage.end() : len(instructions) if following is None else following.start()]
        for stage, following in itertools.zip_longest(stages, stages[1:])
        if stage['stage'] == _GUEST_STAGE
    ]
    assert len(bodies) == 1, f'the Dockerfile does not declare exactly one `{_GUEST_STAGE}` stage'
    return bodies[0]


def _landing(source: str, target: str) -> dict[str, pathlib.Path]:
    """Where `source` lands under site-packages: guest-side path → repo path, one entry per Python file.

    Args:
        source: A COPY source, relative to the build context (the repo root).
        target: The COPY destination with the site-packages prefix stripped; a trailing `/` means a directory.
    """
    path = _REPO_ROOT / source
    if path.is_dir():  # a directory source lands as its contents, under the destination
        return {f'{target.rstrip("/")}/{found.relative_to(path).as_posix()}': found for found in path.rglob('*.py')}
    assert path.is_file(), f'the Dockerfile copies {source}, which is not a literal file path in the repo'
    return {target + path.name if target.endswith('/') else target: path}


def _guest_modules() -> dict[str, pathlib.Path]:
    """Every Python file the guest stage copies into site-packages: guest-side import path → repo path."""
    modules: dict[str, pathlib.Path] = {}
    for copy in _COPY.finditer(_guest_stage()):
        tokens = copy['argv'].split()
        *sources, destination = [token for token in tokens if not token.startswith('--')]
        if _SITE_PACKAGES not in destination:
            continue
        # `--from` sources name another stage's filesystem, so they resolve against no path in the repo.
        assert not any(token.startswith('--from=') for token in tokens), f'guest COPY from a stage: {copy["argv"]}'
        for source in sources:
            modules.update(_landing(source, destination.split(_SITE_PACKAGES, 1)[1]))
    return modules


def _imported_module(package: str, name: str) -> str:
    """What `from <package> import <name>` needs: the submodule or subpackage if there is one, else the package.

    Which of the three it is cannot be read off the statement, so it is decided against the repo — the same
    source the COPY paths are resolved against. Falling back to the package is only right for a symbol: for a
    module or a subpackage it would be satisfied by the parent's landing, which passes over a rootfs that
    breaks. A package the repo does not hold (`themis.agent`, which the guest assembles rather than copies) is
    refused rather than answered: the fallback would be satisfied by a sibling's own landing.
    """
    directory = _REPO_ROOT / package.replace('.', '/')
    assert directory.is_dir(), f'`from {package} import {name}` names a package the repo does not hold'
    if (directory / f'{name}.py').is_file() or (directory / name).is_dir():
        return f'{package}.{name}'
    return package


def _first_party_imports(module: pathlib.Path) -> set[str]:
    """Every `themis` module `module` imports, dotted.

    A relative import is refused rather than skipped: what it resolves to depends on where the module lands,
    not where the repo keeps it, and the guest renames as it copies — `guest/services.py` lands as
    `themis/agent/services.py`, so a sibling that sits beside it in the repo is nowhere in the rootfs.
    Skipping one would contribute no requirement and let the closure pass over a guest that cannot import.
    """
    required: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text('utf-8'))):
        if isinstance(node, ast.ImportFrom):
            # Checked before the module test: `from . import x` carries no module at all.
            assert node.level == 0, f'{module} imports relatively, which resolves against the landing path'
            if node.module is not None and (node.module == 'themis' or node.module.startswith('themis.')):
                required.update(_imported_module(node.module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            required.update(alias.name for alias in node.names if alias.name.split('.')[0] == 'themis')
    return required


def _resolves(dotted: str, landings: frozenset[str]) -> bool:
    """Whether the guest ships the named module — as a file, or as a package with anything under it.

    A directory counts without an `__init__`: the packages here are namespace packages, so requiring one
    would report a package the guest ships as unshipped.
    """
    path = dotted.replace('.', '/')
    return (
        f'{path}.py' in landings
        or f'{path}/__init__.py' in landings
        or any(landing.startswith(f'{path}/') for landing in landings)
    )


def test_the_guest_ships_what_its_modules_import() -> None:
    guest = _guest_modules()
    landings = frozenset(guest)
    required = {landing: _first_party_imports(source) for landing, source in sorted(guest.items())}
    # Both guards rule out a vacuous pass: nothing shipped, or nothing importing, satisfies the closure trivially.
    assert landings, 'no modules parsed out of the guest COPY instructions'
    assert any(required.values()), 'no guest module parsed as importing themis'
    for landing, imports in required.items():
        unresolved = sorted(name for name in imports if not _resolves(name, landings))
        assert not unresolved, f'{landing} imports {unresolved}, which the guest rootfs does not ship'
