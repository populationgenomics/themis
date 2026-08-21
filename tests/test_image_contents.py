"""Every module an image's entrypoint imports has to be importable inside that image.

The service Dockerfiles ship hand-listed subtrees, not the whole repo, so an import edge added to
reachable code leaves the image incomplete: `uv sync` installs the locked dependencies, not the
repo's own tree, and nothing in the build imports the entrypoint unless that Dockerfile says so.
The miss surfaces as a crash-looping revision after the deploy. This test walks the entrypoint's
transitive first-party imports statically and fails on one no `COPY` puts on the import path — for a
namespace package, which has no file of its own, that path is the directory the name resolves to.

Four things stay out of reach. A data file a module reads beside itself, a module reached through a
computed `importlib` string, and a `RUN` that deletes what a `COPY` put there: the walk follows
`import` statements, and what lands in the image is read off `COPY` and `WORKDIR` alone. Fourth, a
second import root inside the image — `themis/services/sandbox_worker/Dockerfile` builds a guest SDK
into a sibling stage's site-packages and ships it as a rootfs, and what imports it is code the model
writes at runtime, named in no Dockerfile. A `.dockerignore` would make the filesystem the wrong
place to read the build context from, so it raises rather than going unmodelled.

One thing is assumed rather than read: a `COPY --from=` is taken to land a directory, because what
another stage holds does not follow from this file. Where that decides whether a later single-file
`COPY` lands inside the destination or takes its name, the image reads as holding the more complete
of the two.

The rest of the file tests the Dockerfile and import parsing the walk rests on, against synthetic
inputs.
"""

from __future__ import annotations

import ast
import collections
import dataclasses
import json
import pathlib
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_IMAGES = tuple(json.loads((_REPO_ROOT / '.github' / 'images.json').read_text('utf-8')))

# Where the Python images put the repo tree, and so where a module has to land to be importable.
_APP = pathlib.PurePosixPath('/app')

# The `runtime` an entry declares. Only a Python image has an import path a `COPY` can miss, and the
# declaration decides which is which: inferred from the Dockerfile, an image that reaches Python
# through a wrapper script reads the same as one running no Python at all, and goes unwalked.
# `test_images_manifest.py` binds the vocabulary, so a runtime this does not handle cannot be
# declared; it raises rather than skipping if one is anyway.
_PYTHON_RUNTIME = 'python'
_RUNTIMES_WITHOUT_A_PYTHON_IMPORT_PATH = frozenset({'bun'})

# Dockerfile's instruction set is closed, so an unrecognised keyword is a typo — and one silently
# read as an instruction that ships nothing would understate what the image holds.
_KEYWORDS = frozenset(
    {
        'ADD',
        'ARG',
        'CMD',
        'COPY',
        'ENTRYPOINT',
        'ENV',
        'EXPOSE',
        'FROM',
        'HEALTHCHECK',
        'LABEL',
        'MAINTAINER',
        'ONBUILD',
        'RUN',
        'SHELL',
        'STOPSIGNAL',
        'USER',
        'VOLUME',
        'WORKDIR',
    }
)

# COPY flags that leave the source operands naming what they say; `--parents` and `--exclude` do not.
_OPERAND_PRESERVING_FLAGS = ('--chown=', '--chmod=', '--link', '--from=')

# Names an exclusion file takes: at the context root, and BuildKit's per-Dockerfile `<name>` + this.
# What a COPY carries is read off the filesystem here, so an excluded module would read as shipped —
# the one shape that could pass an image the deploy then crash-loops. Neither is modelled.
_DOCKERIGNORE = '.dockerignore'

_GLOB_CHARACTERS = '*?['

# `RUN <<EOF`, `COPY <<-"EOF" file`. A shell redirection reads `a << b`, with a space after the `<<`.
_HEREDOC = re.compile(r'<<-?[\'"]?[A-Za-z_]')

# A parser directive, which only appears before the first instruction.
_ESCAPE_DIRECTIVE = re.compile(r'^#\s*escape\s*=')


@dataclasses.dataclass(frozen=True)
class _Stage:
    """One `FROM` block: the image or stage it builds on, and the instructions it adds."""

    name: str | None
    base: str
    instructions: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class _Copy:
    """One source operand of a `COPY`, and where what it carries lands in the image."""

    source: pathlib.PurePosixPath | None
    """Repo-relative path read from the build context; `None` for a `COPY --from=`."""

    source_is_directory: bool
    destination: pathlib.PurePosixPath
    destination_is_directory: bool


def _instructions(dockerfile: pathlib.Path) -> list[tuple[str, str]]:
    """Split a Dockerfile into (keyword, argument) pairs, one per logical instruction.

    Args:
        dockerfile: Absolute path to the Dockerfile.

    Returns:
        The instructions in file order, line continuations joined and comments dropped.

    Raises:
        ValueError: On an `escape` directive, a heredoc, an unrecognised keyword, or a file ending
            mid-continuation — shapes whose instruction boundaries or content this does not read.
    """
    pairs: list[tuple[str, str]] = []
    pending = ''
    for raw in dockerfile.read_text('utf-8').splitlines():
        line = raw.strip()
        if line.startswith('#'):
            before_the_first_instruction = not pairs and not pending
            if before_the_first_instruction and _ESCAPE_DIRECTIVE.match(line):
                raise ValueError(f'{dockerfile}: an `escape` directive changes the continuation character')
            continue  # a comment may also sit between two continued lines
        if not line:
            continue
        if _HEREDOC.search(line):
            raise ValueError(f'{dockerfile}: heredoc instructions are not parsed')
        pending += line
        if pending.endswith('\\'):
            pending = f'{pending[:-1]} '
            continue
        keyword, _, argument = pending.partition(' ')
        if keyword.upper() not in _KEYWORDS:
            raise ValueError(f'{dockerfile}: {keyword!r} is not a Dockerfile instruction')
        pairs.append((keyword.upper(), argument.strip()))
        pending = ''
    if pending:
        raise ValueError(f'{dockerfile}: the last instruction ends in a line continuation')
    return pairs


def _words(argument: str, dockerfile: pathlib.Path) -> list[str]:
    """An instruction argument as words, in either the exec (JSON) or the shell form.

    Raises:
        ValueError: If the exec form holds a non-string.
    """
    if not argument.startswith('['):
        return shlex.split(argument)
    exec_form = json.loads(argument)
    if not all(isinstance(word, str) for word in exec_form):
        raise ValueError(f'{dockerfile}: exec form {argument!r} holds a non-string')
    return [str(word) for word in exec_form]


def _stages(dockerfile: pathlib.Path) -> list[_Stage]:
    """The Dockerfile's `FROM` blocks, in file order.

    Raises:
        ValueError: If the file has no `FROM`, puts something other than an `ARG` before the first
            one, or writes a `FROM` in a form other than `FROM [flags] <image> [AS <name>]`.
    """
    instructions = _instructions(dockerfile)
    starts = [index for index, (keyword, _) in enumerate(instructions) if keyword == 'FROM']
    if not starts:
        raise ValueError(f'{dockerfile}: no FROM instruction')
    # Only ARG may precede the first FROM, and it declares a build argument rather than image content.
    preamble = {keyword for keyword, _ in instructions[: starts[0]]} - {'ARG'}
    if preamble:
        raise ValueError(f'{dockerfile}: {sorted(preamble)} precedes the first FROM')
    stages = []
    for start, end in zip(starts, [*starts[1:], len(instructions)], strict=True):
        header = instructions[start][1]
        words = [word for word in _words(header, dockerfile) if not word.startswith('--')]
        block = tuple(instructions[start + 1 : end])
        if len(words) == 3 and words[1].upper() == 'AS':
            stages.append(_Stage(words[2], words[0], block))
        elif len(words) == 1:
            stages.append(_Stage(None, words[0], block))
        else:
            raise ValueError(f'{dockerfile}: FROM {header!r} is not `FROM [flags] <image> [AS <name>]`')
    return stages


def _image_chain(dockerfile: pathlib.Path) -> list[_Stage]:
    """The stages a plain `docker build` produces, ancestors first.

    The last `FROM` is the stage a build with no `--target` produces, and a stage carries the `ENV`,
    `COPY` and command of the stage it names in `FROM`. So the ancestors come first and the final
    stage overrides them; a `FROM` naming an image from outside the file ends the chain.

    Raises:
        ValueError: If the `FROM` chain is circular.
    """
    stages = _stages(dockerfile)
    by_name = {stage.name: stage for stage in stages if stage.name is not None}
    chain = [stages[-1]]
    while chain[0].base in by_name:
        inherited = by_name[chain[0].base]
        if inherited in chain:
            raise ValueError(f'{dockerfile}: the FROM chain through {inherited.name} is circular')
        chain.insert(0, inherited)
    return chain


def _image_instructions(dockerfile: pathlib.Path) -> list[tuple[str, str]]:
    """Every instruction in force in the image a plain `docker build` produces, in chain order.

    Raises:
        ValueError: If the `FROM` chain is circular.
    """
    return [instruction for stage in _image_chain(dockerfile) for instruction in stage.instructions]


def _last_argument(instructions: Iterable[tuple[str, str]], keyword: str) -> str | None:
    """The argument of the last instruction with this keyword — a later one overrides an earlier."""
    arguments = [argument for instruction, argument in instructions if instruction == keyword]
    return arguments[-1] if arguments else None


def _environment(instructions: Iterable[tuple[str, str]], dockerfile: pathlib.Path) -> dict[str, str]:
    """The image's `ENV` assignments.

    Raises:
        ValueError: On the legacy `ENV name value` form, which this does not parse.
    """
    values: dict[str, str] = {}
    for keyword, argument in instructions:
        if keyword != 'ENV':
            continue
        for word in _words(argument, dockerfile):
            name, assigned, value = word.partition('=')
            if not assigned:
                raise ValueError(f'{dockerfile}: ENV word {word!r} without `=` is not parsed')
            values[name] = value
    return values


def _walks_the_import_path(runtime: str, source: str) -> bool:
    """Whether an image declaring this runtime has a Python import path a `COPY` could miss.

    Raises:
        ValueError: On a runtime this does not handle. `test_images_manifest.py` binds the vocabulary,
            so one arriving here is a gap between the two files — never a reason to skip the image.
    """
    if runtime == _PYTHON_RUNTIME:
        return True
    if runtime in _RUNTIMES_WITHOUT_A_PYTHON_IMPORT_PATH:
        return False
    raise ValueError(f'{source}: images.json declares runtime {runtime!r}, which this does not handle')


def _import_root(
    instructions: Iterable[tuple[str, str]],
    dockerfile: pathlib.Path,
    context: str,
) -> pathlib.PurePosixPath:
    """The image path a `python -m` module name resolves against.

    Args:
        instructions: The instructions in force in the image.
        dockerfile: Absolute path to the Dockerfile, for error messages.
        context: The build context, repo-relative, as `.github/images.json` declares it.

    Returns:
        The directory the module names resolve under.

    Raises:
        ValueError: If the build context is not the repo root, or `PYTHONPATH` does not carry it —
            either way a `COPY` destination no longer decides whether a module is importable.
    """
    if context != '.':
        raise ValueError(f'{dockerfile}: the build context is {context!r}, so {_APP} does not hold the repo tree')
    declared = _environment(instructions, dockerfile).get('PYTHONPATH')
    if declared is None or str(_APP) not in declared.split(':'):
        raise ValueError(f'{dockerfile}: PYTHONPATH={declared!r} does not put {_APP} on the import path')
    return _APP


def _entrypoint_module(dockerfile: pathlib.Path) -> str:
    """The module name the image's command runs.

    The command is the image's `ENTRYPOINT` followed by its `CMD`, each the last one the chain
    declares. A stage that declares an `ENTRYPOINT` resets the `CMD` it inherited, so only a `CMD`
    that stage writes itself — before the `ENTRYPOINT` or after it — survives into the command.

    Raises:
        ValueError: If the image declares no command, or the command is not `python -m <module>`.
    """
    entrypoint: list[str] = []
    cmd: list[str] = []
    for stage in _image_chain(dockerfile):
        declared_entrypoint = _last_argument(stage.instructions, 'ENTRYPOINT')
        if declared_entrypoint is not None:
            entrypoint = _words(declared_entrypoint, dockerfile)
            cmd = []
        declared_cmd = _last_argument(stage.instructions, 'CMD')
        if declared_cmd is not None:
            cmd = _words(declared_cmd, dockerfile)
    command = entrypoint + cmd
    if not command:
        raise ValueError(f'{dockerfile}: no CMD or ENTRYPOINT; one from an image outside this file is not read')
    if not pathlib.PurePosixPath(command[0]).name.startswith('python'):
        raise ValueError(f'{dockerfile}: the command {shlex.join(command)!r} runs no Python interpreter')
    if '-m' not in command[1:]:
        raise ValueError(f'{dockerfile}: the command {shlex.join(command)!r} is not `python -m <module>`')
    flag = command.index('-m', 1)
    if flag + 1 == len(command):
        raise ValueError(f'{dockerfile}: the command {shlex.join(command)!r} names no module after `-m`')
    return command[flag + 1]


def _copies(root: pathlib.Path, instructions: Iterable[tuple[str, str]], dockerfile: pathlib.Path) -> list[_Copy]:
    """Where each of the image's `COPY` operands puts what it carries.

    Args:
        root: The build context, which for these images is the repo root.
        instructions: The instructions in force in the image.
        dockerfile: Absolute path to the Dockerfile, for error messages.

    Returns:
        One entry per source operand, in file order.

    Raises:
        ValueError: On a `.dockerignore`, `ADD`, a `WORKDIR` relative to the one before it, a `COPY`
            flag that changes what the operands name, a `COPY` missing a source or a destination, a
            globbed source, a source absent from the build context, or a relative destination with no
            `WORKDIR` ahead of it.
    """
    for ignore in (root / _DOCKERIGNORE, pathlib.Path(f'{dockerfile}{_DOCKERIGNORE}')):
        if ignore.exists():
            raise ValueError(
                f'{ignore} keeps paths out of the build context, which this reads from the filesystem, '
                f'so a module it excludes would read as shipped'
            )
    copies: list[_Copy] = []
    workdir: pathlib.PurePosixPath | None = None
    for keyword, argument in instructions:
        if keyword == 'WORKDIR':
            workdir = pathlib.PurePosixPath(argument)
            if not workdir.is_absolute():
                raise ValueError(f'{dockerfile}: WORKDIR {argument!r} is relative, which this does not track')
            continue
        if keyword == 'ADD':
            raise ValueError(f'{dockerfile}: ADD is not parsed, so what the image holds would be understated')
        if keyword != 'COPY':
            continue
        words = _words(argument, dockerfile)
        flags = [word for word in words if word.startswith('--')]
        unparsed = [flag for flag in flags if not flag.startswith(_OPERAND_PRESERVING_FLAGS)]
        if unparsed:
            raise ValueError(f'{dockerfile}: COPY flag(s) {unparsed} change what the operands name')
        operands = [word for word in words if not word.startswith('--')]
        if len(operands) < 2:
            raise ValueError(f'{dockerfile}: COPY {argument!r} lacks a source, a destination, or both')
        destination = pathlib.PurePosixPath(operands[-1])
        if not destination.is_absolute():
            if workdir is None:
                raise ValueError(f'{dockerfile}: COPY {argument!r} has a relative destination and no WORKDIR')
            destination = workdir / destination
        named_directory = len(operands) > 2 or operands[-1].endswith('/')
        reads_a_stage = any(flag.startswith('--from=') for flag in flags)
        for operand in operands[:-1]:
            if any(character in operand for character in _GLOB_CHARACTERS):
                raise ValueError(f'{dockerfile}: COPY source {operand!r} is a glob; only literal paths are matched')
            if reads_a_stage:
                copies.append(_Copy(None, False, destination, True))
                continue
            source = pathlib.PurePosixPath(operand)
            if (root / source).is_dir():
                is_directory = True
            elif (root / source).is_file():
                is_directory = False
            else:
                raise ValueError(f'{dockerfile}: COPY source {operand!r} is not in the build context')
            into_directory = named_directory or is_directory or _already_a_directory(destination, copies, workdir, root)
            copies.append(_Copy(source, is_directory, destination, into_directory))
    return copies


def _already_a_directory(
    path: pathlib.PurePosixPath,
    copies: Sequence[_Copy],
    workdir: pathlib.PurePosixPath | None,
    root: pathlib.Path,
) -> bool:
    """Whether the image holds this path as a directory by the time a later `COPY` names it.

    Docker's destination is a directory if it ends in a slash, if the `COPY` has several sources, or
    if it *already exists* as one — in which case a single file operand lands inside it rather than
    taking its name. What an earlier `COPY` put there and the `WORKDIR` are knowable from the file; a
    directory a `RUN` made is not.
    """
    if workdir is not None and (path == workdir or path in workdir.parents):
        return True
    return _copies_make_a_directory(path, copies, root)


def _copies_make_a_directory(
    path: pathlib.PurePosixPath,
    copies: Sequence[_Copy],
    root: pathlib.Path,
) -> bool:
    """Whether these `COPY`s leave the image holding this path as a directory.

    A `COPY` landing at or under the path makes it one, as does one whose source tree carries it.
    """
    for copy in copies:
        if path in copy.destination.parents:
            return True  # a COPY landed something under `path`
        if path == copy.destination and copy.destination_is_directory:
            return True
        # `path` sits inside a directory operand's destination, so the source's own shape decides.
        if (
            copy.destination in path.parents
            and copy.source is not None
            and copy.source_is_directory
            and (root / copy.source / path.relative_to(copy.destination)).is_dir()
        ):
            return True
    return False


def _lands_at(copy: _Copy, file: pathlib.PurePosixPath) -> pathlib.PurePosixPath | None:
    """Where a repo-relative file ends up in the image, or `None` if this `COPY` does not carry it."""
    if copy.source is None:
        return None
    if copy.source_is_directory:
        # A directory operand copies the directory's *contents* into the destination, not itself.
        if copy.source != file and copy.source not in file.parents:
            return None
        return copy.destination / file.relative_to(copy.source)
    if copy.source != file:
        return None
    return copy.destination / copy.source.name if copy.destination_is_directory else copy.destination


def _source_files(root: pathlib.Path, module: str) -> list[pathlib.PurePosixPath]:
    """The repo-relative files Python reads to import a module name.

    Args:
        root: The repo root the name resolves against.
        module: Dotted module name.

    Returns:
        The `__init__.py` of every package on the way down, then the module's own file. Empty for a
        name the repo root does not hold, and for a namespace package, which has no file of its own.
    """
    parts = module.split('.')
    files = [
        package
        for depth in range(1, len(parts) + 1)
        if (root / (package := pathlib.PurePosixPath(*parts[:depth], '__init__.py'))).is_file()
    ]
    leaf = pathlib.PurePosixPath(*parts).with_suffix('.py')
    if (root / leaf).is_file():
        files.append(leaf)
    return files


def _is_first_party(root: pathlib.Path, module: str) -> bool:
    """Whether a module name resolves inside the repo, which `PYTHONPATH` searches ahead of the venv.

    A third-party name resolves in the venv instead, where the lock — not a `COPY` — puts it. The
    repo root holds more than `themis`: the vendored `buf.validate` stubs the generated proto
    modules import are reached the same way, and need the same `COPY`.
    """
    path = root / pathlib.PurePosixPath(*module.split('.'))
    return path.is_dir() or path.with_suffix('.py').is_file()


def _imported_names(root: pathlib.Path, file: pathlib.PurePosixPath) -> set[str]:
    """Every module name a source file imports, including names that turn out to be symbols.

    A `from x import y` gives both `x` and `x.y`, because `y` may be either a module or a symbol
    defined in `x`; the caller drops whichever does not resolve. Imports nested in a function count:
    a deferred import runs on the path that needs it.

    Raises:
        ValueError: On a relative import this cannot resolve.
    """
    tree = ast.parse((root / file).read_text('utf-8'), filename=str(file))
    package = '.'.join(file.parts[:-1])
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_base(node, package, file)
            names.add(base)
            names.update(f'{base}.{alias.name}' for alias in node.names if alias.name != '*')
    return names


def _absolute_base(node: ast.ImportFrom, package: str, file: pathlib.PurePosixPath) -> str:
    """The absolute module a `from ... import ...` reads, resolving the relative forms.

    Args:
        node: The import statement.
        package: Dotted name of the package holding `file` — the same for a module and for that
            package's own `__init__.py`, which is what a single leading dot means in both.
        file: Repo-relative path of the importing file, for error messages.

    Raises:
        ValueError: If the import names neither a module nor a level, or climbs past the repo root.
    """
    if not node.level:
        if node.module is None:
            raise ValueError(f'{file}: a `from` import with neither a module nor a leading dot')
        return node.module
    parts = package.split('.') if package else []
    if node.level > len(parts):
        raise ValueError(f'{file}: relative import of level {node.level} climbs above the repo root')
    climbed = parts[: len(parts) - node.level + 1]
    return '.'.join(climbed + ([node.module] if node.module else []))


def _reachable(root: pathlib.Path, entrypoint_module: str) -> dict[str, tuple[str, ...]]:
    """Walk the first-party imports out from the entrypoint module.

    Args:
        root: The repo root module names resolve against.
        entrypoint_module: The module the image's command runs.

    Returns:
        Every first-party module name reached, mapped to the import chain that reaches it — the
        entrypoint first, the module itself last. Breadth-first, so each chain is a shortest one.
    """
    chains: dict[str, tuple[str, ...]] = {entrypoint_module: (entrypoint_module,)}
    queue = collections.deque([entrypoint_module])
    while queue:
        module = queue.popleft()
        for file in _source_files(root, module):
            for imported in sorted(_imported_names(root, file)):
                if imported in chains or not _is_first_party(root, imported):
                    continue
                chains[imported] = (*chains[module], imported)
                queue.append(imported)
    return chains


def _executed_module(root: pathlib.Path, named: str, dockerfile: pathlib.Path) -> str:
    """The module `python -m <named>` executes: a package's `__main__`, or the module itself.

    Raises:
        ValueError: If the name has no source file in the repo.
    """
    module = f'{named}.__main__' if (root / pathlib.PurePosixPath(*named.split('.'))).is_dir() else named
    if not _source_files(root, module):
        raise ValueError(f'{dockerfile}: the entrypoint module {module} has no source file in the repo')
    return module


def _reaches_the_import_path(
    root: pathlib.Path,
    import_root: pathlib.PurePosixPath,
    reached: pathlib.PurePosixPath,
    copies: Sequence[_Copy],
    source: str,
) -> bool:
    """Whether a `COPY` puts a path a module needs where the image's import of it would look.

    A module's source file has to land under the import root at its own repo-relative name. A
    namespace package has no source file, so what it needs is the directory the name resolves to —
    which any `COPY` landing at or under it, or carrying it inside a source tree, brings.

    Args:
        root: The repo root module names resolve against.
        import_root: The image path those names resolve under.
        reached: The repo-relative file, or namespace-package directory, the module needs.
        copies: Where the image's `COPY` operands land.
        source: The Dockerfile's repo-relative path, for error messages.

    Raises:
        ValueError: If a `COPY --from=` lands where the path would have to be. Its source is a path
            in another stage, so whether the path is under it does not follow from this file, and
            passing over it is only sound where it cannot be the one shipping the module. The
            question is asked only of a path no context `COPY` accounts for, and ignores instruction
            order, so a stage copy that lands *over* one already shipped is passed over too.
    """
    importable = import_root / reached
    if (root / reached).is_dir():  # the namespace-package case: only this path's existence is asked
        shipped = _copies_make_a_directory(importable, copies, root)
    else:
        shipped = any(_lands_at(copy, reached) == importable for copy in copies)
    if shipped:
        return True
    for copy in copies:
        if copy.source is None and (copy.destination == importable or copy.destination in importable.parents):
            raise ValueError(
                f'{source}: a COPY --from= into {copy.destination} may be what ships {reached}, and what a '
                f'stage holds does not follow from this file'
            )
    return False


def _unshipped(
    root: pathlib.Path,
    import_root: pathlib.PurePosixPath,
    chains: Mapping[str, tuple[str, ...]],
    copies: Sequence[_Copy],
    source: str,
) -> dict[pathlib.PurePosixPath, tuple[str, ...]]:
    """The reached paths no `COPY` puts on the image's import path, with the chain reaching each.

    Args:
        root: The repo root module names resolve against.
        import_root: The image path those names resolve under.
        chains: Reachable module names, mapped to the import chain reaching each.
        copies: Where the image's `COPY` operands land.
        source: The Dockerfile's repo-relative path, for error messages.

    Returns:
        One entry per absent path, along the shortest chain that reaches it: the file a module is
        read from — a package's `__init__.py` is a source file of every module beneath it — or, for
        a namespace package, the directory the name resolves to.

    Raises:
        ValueError: If a `COPY --from=` may be what ships a path none of the context `COPY`s
            account for.
    """
    absent: dict[pathlib.PurePosixPath, tuple[str, ...]] = {}
    for module, chain in chains.items():
        files = _source_files(root, module)
        # A namespace package has no source file; its directory is what has to reach the image.
        reached = files if files else [pathlib.PurePosixPath(*module.split('.'))]
        for path in reached:
            if not _reaches_the_import_path(root, import_root, path, copies, source):
                absent.setdefault(path, chain)
    return absent


@pytest.mark.parametrize('image', _IMAGES, ids=[image['name'] for image in _IMAGES])
def test_every_module_the_entrypoint_imports_is_shipped(image: dict[str, str]) -> None:
    runtime = image.get('runtime')
    assert runtime is not None, f'images.json entry {image["name"]!r} declares no runtime'
    if not _walks_the_import_path(runtime, image['file']):
        pytest.skip(f'a {runtime} image has no Python import path for a COPY to miss')
    dockerfile = _REPO_ROOT / image['file']
    instructions = _image_instructions(dockerfile)
    import_root = _import_root(instructions, dockerfile, image['context'])
    module = _entrypoint_module(dockerfile)
    chains = _reachable(_REPO_ROOT, _executed_module(_REPO_ROOT, module, dockerfile))
    # A resolver that quietly stopped at the entrypoint would pass every image here.
    assert len(chains) > 1, f'the walk from {module} reached no first-party import'
    copies = _copies(_REPO_ROOT, instructions, dockerfile)
    absent = _unshipped(_REPO_ROOT, import_root, chains, copies, image['file'])
    assert not absent, '\n'.join(
        f'{file} is on no import path {image["file"]} builds\n  imported along {" -> ".join(chain)}'
        for file, chain in sorted(absent.items())
    )


# The resolving above, against synthetic inputs.

_PYTHON_IMAGE = 'FROM python:3.13-slim AS runtime\nWORKDIR /app\nENV PYTHONPATH=/app\n'


def _write(root: pathlib.Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, 'utf-8')


def _absent(root: pathlib.Path, dockerfile_body: str) -> list[str]:
    """The synthetic image's verdict: the root-relative files it leaves off the import path."""
    dockerfile = root / 'Dockerfile'
    dockerfile.write_text(f'{_PYTHON_IMAGE}{dockerfile_body}', 'utf-8')
    instructions = _image_instructions(dockerfile)
    import_root = _import_root(instructions, dockerfile, '.')
    module = _entrypoint_module(dockerfile)
    chains = _reachable(root, _executed_module(root, module, dockerfile))
    copies = _copies(root, instructions, dockerfile)
    return [str(file) for file in _unshipped(root, import_root, chains, copies, 'Dockerfile')]


def test_the_walk_follows_imports_transitively(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        {
            'pkg/__main__.py': 'import pkg.a\n',
            'pkg/a.py': 'import pkg.b\n',
            'pkg/b.py': 'import pkg.c\n',
            'pkg/c.py': '',
        },
    )
    assert _reachable(tmp_path, 'pkg.__main__')['pkg.c'] == ('pkg.__main__', 'pkg.a', 'pkg.b', 'pkg.c')


def test_the_walk_counts_an_import_deferred_into_a_function(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': 'def serve():\n    import pkg.late\n', 'pkg/late.py': ''})
    assert 'pkg.late' in _reachable(tmp_path, 'pkg.__main__')


def test_a_from_import_of_a_symbol_resolves_to_the_module_defining_it(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/a.py': 'from pkg.b import Thing\n', 'pkg/b.py': 'class Thing: ...\n'})
    assert set(_reachable(tmp_path, 'pkg.a')) == {'pkg.a', 'pkg.b'}


def test_a_from_import_of_a_module_resolves_to_that_module(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/a.py': 'from pkg import b\n', 'pkg/b.py': ''})
    assert set(_reachable(tmp_path, 'pkg.a')) == {'pkg.a', 'pkg', 'pkg.b'}


@pytest.mark.parametrize(
    ('importing', 'expected'),
    [
        ('from . import sibling\n', 'pkg.inner.sibling'),
        ('from .sibling import Thing\n', 'pkg.inner.sibling'),
        ('from .. import outer\n', 'pkg.outer'),
        ('from ..outer import Thing\n', 'pkg.outer'),
    ],
)
def test_a_relative_import_resolves_against_the_containing_package(
    tmp_path: pathlib.Path,
    importing: str,
    expected: str,
) -> None:
    _write(tmp_path, {'pkg/inner/a.py': importing, 'pkg/inner/sibling.py': '', 'pkg/outer.py': ''})
    assert expected in _reachable(tmp_path, 'pkg.inner.a')


@pytest.mark.parametrize(
    ('module', 'files'),
    [
        ('pkg.a', {'pkg/a.py': 'from ... import nothing\n'}),
        # A module at the root has no containing package for a single dot to mean.
        ('a', {'a.py': 'from . import b\n', 'b.py': ''}),
    ],
)
def test_a_relative_import_above_the_repo_root_raises(
    tmp_path: pathlib.Path,
    module: str,
    files: Mapping[str, str],
) -> None:
    _write(tmp_path, files)
    with pytest.raises(ValueError, match='climbs above the repo root'):
        _reachable(tmp_path, module)


def test_stdlib_and_third_party_imports_are_not_followed(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/a.py': 'import json\nimport grpc.aio\nfrom collections.abc import Iterable\n'})
    assert set(_reachable(tmp_path, 'pkg.a')) == {'pkg.a'}


def test_every_package_init_on_the_way_down_is_a_source_file(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__init__.py': '', 'pkg/sub/__init__.py': '', 'pkg/sub/leaf.py': ''})
    assert _source_files(tmp_path, 'pkg.sub.leaf') == [
        pathlib.PurePosixPath('pkg/__init__.py'),
        pathlib.PurePosixPath('pkg/sub/__init__.py'),
        pathlib.PurePosixPath('pkg/sub/leaf.py'),
    ]


def test_a_namespace_package_contributes_no_source_file(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/sub/leaf.py': ''})
    assert _source_files(tmp_path, 'pkg.sub.leaf') == [pathlib.PurePosixPath('pkg/sub/leaf.py')]


@pytest.mark.parametrize(
    ('copies', 'absent'),
    [
        # Nothing lands under `/app/ns`, so the deployed image raises ModuleNotFoundError on it.
        ('COPY pkg ./pkg', ['ns']),
        ('COPY pkg ./pkg\nCOPY ns ./ns', []),
        # The directory is all the name needs; a COPY that creates it in passing is enough.
        ('COPY pkg ./pkg\nCOPY ns/leaf.py ./ns/', []),
        ('COPY . .', []),
    ],
)
def test_a_namespace_package_needs_its_directory_in_the_image(
    tmp_path: pathlib.Path,
    copies: str,
    absent: list[str],
) -> None:
    # `ns` has no `__init__.py`, so it contributes no source file for a COPY to be measured against.
    _write(tmp_path, {'pkg/__main__.py': 'import ns\n', 'ns/leaf.py': ''})
    assert _absent(tmp_path, f'{copies}\nCMD ["python", "-m", "pkg"]\n') == absent


@pytest.mark.parametrize(
    ('copy', 'absent'),
    [
        ('COPY pkg ./pkg', []),
        ('COPY . .', []),
        ('COPY pkg /app/pkg', []),
        ('COPY pkg/__main__.py pkg/a.py ./pkg/', []),
        ('COPY pkg/__main__.py ./pkg/__main__.py\nCOPY pkg/a.py ./pkg/', []),
        # A directory operand copies the directory's contents, so these land beside `pkg`, not in it.
        ('COPY pkg ./', ['pkg/__main__.py', 'pkg/a.py']),
        ('COPY pkg ./pkg/nested', ['pkg/__main__.py', 'pkg/a.py']),
        ('COPY pkg/__main__.py pkg/a.py ./', ['pkg/__main__.py', 'pkg/a.py']),
        ('COPY pkg/__main__.py ./pkg/renamed.py\nCOPY pkg/a.py ./pkg/', ['pkg/__main__.py']),
        ('COPY pkg/__main__.py ./pkg/', ['pkg/a.py']),
    ],
)
def test_a_copy_ships_a_module_only_where_it_lands_on_the_import_path(
    tmp_path: pathlib.Path,
    copy: str,
    absent: list[str],
) -> None:
    _write(tmp_path, {'pkg/__main__.py': 'import pkg.a\n', 'pkg/a.py': ''})
    assert _absent(tmp_path, f'{copy}\nCMD ["python", "-m", "pkg"]\n') == absent


def test_a_copy_in_a_sibling_stage_does_not_ship(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': 'import pkg.a\n', 'pkg/a.py': ''})
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(
        'FROM python:3.13-slim AS base\nENV PYTHONPATH=/app\nWORKDIR /app\nRUN uv sync\n'
        'FROM base AS build\nCOPY pkg ./pkg\n'
        'FROM base AS runtime\nCOPY pkg/__main__.py ./pkg/\nCMD ["python", "-m", "pkg"]\n',
        'utf-8',
    )
    instructions = _image_instructions(dockerfile)
    chains = _reachable(tmp_path, 'pkg.__main__')
    copies = _copies(tmp_path, instructions, dockerfile)
    assert list(_unshipped(tmp_path, _APP, chains, copies, 'Dockerfile')) == [pathlib.PurePosixPath('pkg/a.py')]


def test_a_copy_inherited_from_the_stage_built_on_does_ship(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': 'import pkg.a\n', 'pkg/a.py': ''})
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(
        'FROM python:3.13-slim AS base\nENV PYTHONPATH=/app\nWORKDIR /app\nRUN uv sync\nCOPY pkg/a.py ./pkg/\n'
        'FROM base AS runtime\nCOPY pkg/__main__.py ./pkg/\nCMD ["python", "-m", "pkg"]\n',
        'utf-8',
    )
    instructions = _image_instructions(dockerfile)
    copies = _copies(tmp_path, instructions, dockerfile)
    assert not _unshipped(tmp_path, _APP, _reachable(tmp_path, 'pkg.__main__'), copies, 'Dockerfile')


def test_a_relative_destination_resolves_against_workdir(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': ''})
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(
        'FROM python:3.13-slim AS runtime\nWORKDIR /srv\nENV PYTHONPATH=/app\nRUN uv sync\n'
        'COPY pkg ./pkg\nCMD ["python", "-m", "pkg"]\n',
        'utf-8',
    )
    instructions = _image_instructions(dockerfile)
    copies = _copies(tmp_path, instructions, dockerfile)
    # The tree lands under /srv, so it is on no import path rooted at /app.
    absent = _unshipped(tmp_path, _APP, _reachable(tmp_path, 'pkg.__main__'), copies, 'Dockerfile')
    assert list(absent) == [pathlib.PurePosixPath('pkg/__main__.py')]


@pytest.mark.parametrize(
    'copies',
    [
        # An earlier COPY has made the destination a directory, so the file lands inside it rather
        # than taking its name — the shape `themis/services/auth/Dockerfile` writes with one COPY.
        'COPY pkg/__main__.py ./pkg/\nCOPY pkg/a.py ./pkg',
        'COPY pkg/a.py ./pkg/\nCOPY pkg/__main__.py ./pkg',
        # A directory operand makes its destination one too.
        'COPY nested ./pkg\nCOPY pkg/__main__.py ./pkg\nCOPY pkg/a.py ./pkg',
        # An earlier COPY landing deeper makes every directory above it one.
        'COPY extra/note.py ./pkg/sub/\nCOPY pkg/__main__.py ./pkg\nCOPY pkg/a.py ./pkg',
        # A directory operand's own subdirectories arrive with it.
        'COPY nested ./\nCOPY pkg/__main__.py ./pkg\nCOPY pkg/a.py ./pkg',
    ],
)
def test_a_destination_an_earlier_copy_made_a_directory_takes_files_into_it(
    tmp_path: pathlib.Path,
    copies: str,
) -> None:
    _write(
        tmp_path,
        {'pkg/__main__.py': 'import pkg.a\n', 'pkg/a.py': '', 'nested/pkg/keep.py': '', 'extra/note.py': ''},
    )
    assert _absent(tmp_path, f'{copies}\nCMD ["python", "-m", "pkg"]\n') == []


def test_a_destination_that_is_the_workdir_takes_the_file_into_it(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'mod.py': ''})
    # The WORKDIR exists as a directory, so `.` takes the file into it rather than replacing it.
    assert _absent(tmp_path, 'COPY mod.py .\nCMD ["python", "-m", "mod"]\n') == []


def test_a_destination_no_copy_has_made_a_directory_renames_the_file(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': ''})
    # /app/pkg is not a directory yet, so this names the file /app/pkg and nothing is importable.
    assert _absent(tmp_path, 'COPY pkg/__main__.py ./pkg\nCMD ["python", "-m", "pkg"]\n') == ['pkg/__main__.py']


def test_a_copy_source_absent_from_the_build_context_raises(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': ''})
    with pytest.raises(ValueError, match='is not in the build context'):
        _absent(tmp_path, 'COPY pkg/renamed ./pkg/\nCMD ["python", "-m", "pkg"]\n')


def test_a_stage_copy_that_could_be_shipping_the_module_raises(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': ''})
    with pytest.raises(ValueError, match='COPY --from='):
        _absent(tmp_path, 'COPY --from=build /src/pkg ./pkg\nCMD ["python", "-m", "pkg"]\n')


def test_a_stage_copy_landing_clear_of_the_tree_is_passed_over(tmp_path: pathlib.Path) -> None:
    # `pkg/a.py` is absent, so the stage copy is weighed and has to be ruled out rather than raised on.
    _write(tmp_path, {'pkg/__main__.py': 'import pkg.a\n', 'pkg/a.py': ''})
    body = 'COPY --from=build /app/.venv /app/.venv\nCOPY pkg/__main__.py ./pkg/\nCMD ["python", "-m", "pkg"]\n'
    assert _absent(tmp_path, body) == ['pkg/a.py']


@pytest.mark.parametrize(('runtime', 'walks'), [('python', True), ('bun', False)])
def test_the_declared_runtime_decides_whether_the_import_path_is_walked(runtime: str, walks: bool) -> None:
    assert _walks_the_import_path(runtime, 'Dockerfile') is walks


def test_a_declared_runtime_the_walk_does_not_handle_raises() -> None:
    with pytest.raises(ValueError, match='does not handle'):
        _walks_the_import_path('deno', 'Dockerfile')


@pytest.mark.parametrize(
    ('dockerfile', 'message'),
    [
        ('FROM x AS a\nRUN <<EOF\necho hi\nEOF\n', 'heredoc'),
        ('# escape=`\nFROM x AS a\n', 'escape'),
        ('FROM x AS a\nCOPYY pkg ./pkg\n', 'not a Dockerfile instruction'),
        ('FROM x AS a\nCOPY pkg \\\n', 'ends in a line continuation'),
        ('FROM x AS a\nADD pkg /pkg\n', 'ADD is not parsed'),
        ('FROM x AS a\nCOPY --parents pkg /pkg\n', 'change what the operands name'),
        ('FROM x AS a\nCOPY pkg/*.py /pkg/\n', 'is a glob'),
        ('FROM x AS a\nCOPY pkg\n', 'lacks a source'),
        ('FROM x AS a\nCOPY pkg ./pkg\n', 'relative destination and no WORKDIR'),
        ('FROM x AS a\nWORKDIR app\nCOPY pkg ./pkg\n', 'is relative'),
        ('RUN true\nFROM x AS a\n', 'precedes the first FROM'),
        ('ENV A=1\n', 'no FROM instruction'),
        ('FROM x AS a\nFROM y z w AS b\n', 'is not `FROM'),
        ('FROM two AS one\nFROM one AS two\n', 'circular'),
    ],
)
def test_a_dockerfile_shape_the_parser_does_not_model_raises(
    tmp_path: pathlib.Path,
    dockerfile: str,
    message: str,
) -> None:
    (tmp_path / 'Dockerfile').write_text(dockerfile, 'utf-8')
    with pytest.raises(ValueError, match=message):
        _copies(tmp_path, _image_instructions(tmp_path / 'Dockerfile'), tmp_path / 'Dockerfile')


# Both the context-root file and BuildKit's per-Dockerfile one, whose name is the Dockerfile's plus
# the suffix — the whole point being that a walk which missed either would pass an incomplete image.
@pytest.mark.parametrize('ignore', [_DOCKERIGNORE, f'Dockerfile{_DOCKERIGNORE}'])
def test_a_dockerignore_the_parser_does_not_model_raises(tmp_path: pathlib.Path, ignore: str) -> None:
    _write(tmp_path, {'pkg/__main__.py': '', ignore: 'pkg/a.py\n'})
    with pytest.raises(ValueError, match='would read as shipped'):
        _absent(tmp_path, 'COPY pkg ./pkg\nCMD ["python", "-m", "pkg"]\n')


@pytest.mark.parametrize(
    'instruction',
    [
        'RUN test $((1 << 3))',  # a shift, not a heredoc marker
        'RUN echo "a << b"',
        'RUN echo shifted <<3',
    ],
)
def test_a_shell_shift_is_not_read_as_a_heredoc(tmp_path: pathlib.Path, instruction: str) -> None:
    (tmp_path / 'Dockerfile').write_text(f'FROM x AS a\n{instruction}\n', 'utf-8')
    assert _instructions(tmp_path / 'Dockerfile') == [('FROM', 'x AS a'), ('RUN', instruction[len('RUN ') :])]


def test_an_escape_mention_after_the_first_instruction_is_a_comment(tmp_path: pathlib.Path) -> None:
    # Docker reads a parser directive only above the first instruction; below it the line is prose.
    (tmp_path / 'Dockerfile').write_text('FROM x AS a\n# escape=`\nRUN true\n', 'utf-8')
    assert _instructions(tmp_path / 'Dockerfile') == [('FROM', 'x AS a'), ('RUN', 'true')]


@pytest.mark.parametrize(
    ('body', 'message'),
    [
        ('CMD ["python", "app.py"]\n', 'is not `python -m'),
        ('CMD ["python", "-m"]\n', 'names no module'),
        ('', 'no CMD or ENTRYPOINT'),
        # A wrapper script hides the interpreter, so the module it runs is not knowable from here.
        ('CMD ["/app/start.sh"]\n', 'runs no Python interpreter'),
    ],
)
def test_an_entrypoint_that_is_not_python_dash_m_raises(tmp_path: pathlib.Path, body: str, message: str) -> None:
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(f'{_PYTHON_IMAGE}{body}', 'utf-8')
    with pytest.raises(ValueError, match=message):
        _entrypoint_module(dockerfile)


def test_an_entrypoint_drops_the_cmd_its_stage_inherited(tmp_path: pathlib.Path) -> None:
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(
        f'{_PYTHON_IMAGE}CMD ["python", "-m", "inherited"]\nFROM runtime AS final\nENTRYPOINT ["python"]\n',
        'utf-8',
    )
    # The reset leaves `python` alone as the command, which names no module to walk from.
    with pytest.raises(ValueError, match='is not `python -m'):
        _entrypoint_module(dockerfile)


@pytest.mark.parametrize(
    'command',
    ['ENTRYPOINT ["python"]\nCMD ["-m", "pkg"]\n', 'CMD ["-m", "pkg"]\nENTRYPOINT ["python"]\n'],
)
def test_a_cmd_the_entrypoints_own_stage_writes_stands(tmp_path: pathlib.Path, command: str) -> None:
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(f'{_PYTHON_IMAGE}{command}', 'utf-8')
    assert _entrypoint_module(dockerfile) == 'pkg'


@pytest.mark.parametrize(
    ('layout', 'context', 'message'),
    [
        ('ENV PYTHONPATH=/app\n', 'apps/web', 'the build context is'),
        ('', '.', 'does not put /app on the import path'),
        ('ENV PYTHONPATH=/srv\n', '.', 'does not put /app on the import path'),
        ('ENV PYTHONPATH=/application\n', '.', 'does not put /app on the import path'),
        ('ENV OTHER value\n', '.', 'without `=`'),
    ],
)
def test_a_layout_that_does_not_import_off_the_context_raises(
    tmp_path: pathlib.Path,
    layout: str,
    context: str,
    message: str,
) -> None:
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(f'FROM python:3.13-slim AS runtime\nWORKDIR /app\n{layout}', 'utf-8')
    with pytest.raises(ValueError, match=message):
        _import_root(_image_instructions(dockerfile), dockerfile, context)


@pytest.mark.parametrize('declared', ['/app', '/app:/app/vendor', '/opt/extra:/app'])
def test_pythonpath_carrying_the_import_root_among_others_is_accepted(
    tmp_path: pathlib.Path,
    declared: str,
) -> None:
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text(f'FROM python:3.13-slim AS runtime\nENV PYTHONPATH={declared}\n', 'utf-8')
    assert _import_root(_image_instructions(dockerfile), dockerfile, '.') == _APP


def test_an_entrypoint_module_absent_from_the_repo_raises(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, {'pkg/__main__.py': ''})
    with pytest.raises(ValueError, match='has no source file in the repo'):
        _executed_module(tmp_path, 'nonesuch', tmp_path / 'Dockerfile')
