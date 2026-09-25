"""The declaration: an agent's yaml and the skill directories it publishes.

Nothing here reaches the network. The yaml is read and checked for the shape the repo relies on — the
API validates the rest — and a skill directory is read once, as the bytes an upload and its digest share.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import subprocess
from collections.abc import Mapping

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / 'agents'
SKILLS_DIR = AGENTS_DIR / 'skills'
SKILL_FILE = 'SKILL.md'
# The model id the declaration is applied with; the stack's encrypted `themis:anthropicAgentModelId`, never the yaml's.
MODEL_ID_ENV = 'THEMIS_AGENT_MODEL_ID'

_AGENT_KEYS = frozenset({'name', 'model', 'system', 'description', 'tools', 'skills', 'multiagent'})
_SKILL_TYPES = frozenset({'anthropic', 'custom'})
_DIGEST_PREFIX = 'sha256:'

SkillFiles = tuple[tuple[str, bytes], ...]
"""One skill directory's content: (path relative to the directory, bytes), sorted by path."""


class ConfigError(ValueError):
    """The yaml does not declare what it has to, in the shape it has to, or a skill directory is not one skill."""


@dataclasses.dataclass(frozen=True)
class DirectorySkill:
    """A custom skill the repo holds, published from `agents/skills/<directory>` when its content moves."""

    skill_id: str
    directory: str

    @property
    def path(self) -> pathlib.Path:
        return SKILLS_DIR / self.directory


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    """One `agents/<name>.agent.yaml`, read and checked.

    `skills` holds the entries as written: a pinned one carries its `version`, a directory one its
    `directory`, and `directory_skills` names the latter for publishing.
    """

    path: pathlib.Path
    name: str
    model: dict[str, object]
    system: str | None
    description: str | None
    tools: list[dict[str, object]]
    skills: list[dict[str, object]]
    multiagent: dict[str, object] | None

    @property
    def directory_skills(self) -> tuple[DirectorySkill, ...]:
        return tuple(
            DirectorySkill(skill_id=str(entry['skill_id']), directory=str(entry['directory']))
            for entry in self.skills
            if 'directory' in entry
        )


def load(path: pathlib.Path) -> AgentConfig:
    """Read and check one agent declaration.

    The model id is not the yaml's to carry: it is confidential stack config, and the caller supplies
    it at apply. `model` in the yaml holds the model's other settings, `effort` among them.

    Raises:
        ConfigError: If the yaml is not a mapping of the declaration's keys, lacks `name`, names a
            model id, or declares a skill outside the shape `_skill` holds it to.
    """
    raw = yaml.safe_load(path.read_text('utf-8'))
    if not isinstance(raw, dict):
        raise ConfigError(f'{path}: an agent declaration is a mapping')
    unknown = sorted(set(raw) - _AGENT_KEYS)
    if unknown:
        raise ConfigError(f'{path}: unknown keys {unknown}; a declaration takes {sorted(_AGENT_KEYS)}')
    model = _optional_mapping(raw, 'model', path) or {}
    if 'id' in model:
        raise ConfigError(
            f'{path}: `model.id` is confidential stack config (themis:anthropicAgentModelId), supplied at apply as '
            f"{MODEL_ID_ENV}; the yaml carries the model's other settings"
        )
    return AgentConfig(
        path=path,
        name=_required_str(raw, 'name', path),
        model=model,
        system=_optional_str(raw, 'system', path),
        description=_optional_str(raw, 'description', path),
        tools=_mappings(raw, 'tools', path),
        skills=[_skill(entry, path) for entry in _mappings(raw, 'skills', path)],
        multiagent=_optional_mapping(raw, 'multiagent', path),
    )


def _required_str(raw: Mapping[str, object], key: str, path: pathlib.Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{path}: `{key}` is a non-empty string')
    return value


def _optional_str(raw: Mapping[str, object], key: str, path: pathlib.Path) -> str | None:
    if key not in raw:
        return None
    return _required_str(raw, key, path)


def _optional_mapping(raw: Mapping[str, object], key: str, path: pathlib.Path) -> dict[str, object] | None:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, dict):
        raise ConfigError(f'{path}: `{key}` is a mapping')
    return value


def _mappings(raw: Mapping[str, object], key: str, path: pathlib.Path) -> list[dict[str, object]]:
    if key not in raw:
        return []
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigError(f'{path}: `{key}` is a list of mappings')
    return value


def _skill(entry: Mapping[str, object], path: pathlib.Path) -> dict[str, object]:
    """One `skills` entry: a type, a skill id, and one pin.

    The pin is a `version` string, or — for a custom skill the repo holds — the `directory` under
    agents/skills/ whose content is published and pinned at apply.
    """
    kind = entry.get('type')
    if kind not in _SKILL_TYPES:
        raise ConfigError(f'{path}: a skill `type` is one of {sorted(_SKILL_TYPES)}, got {kind!r}')
    skill_id = _required_str(entry, 'skill_id', path)
    allowed = {'type', 'skill_id', 'version'} | ({'directory'} if kind == 'custom' else set())
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ConfigError(f'{path}: skill {skill_id}: unknown keys {unknown}')
    has_version, has_directory = 'version' in entry, 'directory' in entry
    if has_version == has_directory:
        pins = 'a `version` or a `directory`' if kind == 'custom' else 'a `version`'
        raise ConfigError(f'{path}: skill {skill_id} is pinned by exactly one of {pins}')
    if has_version:
        if not isinstance(entry['version'], str):
            raise ConfigError(
                f'{path}: skill {skill_id}: `version` is a string, got {entry["version"]!r} — quote it; '
                'YAML reads a version stamp as an integer'
            )
        version = _required_str(entry, 'version', path)
        if version == 'latest':
            raise ConfigError(
                f'{path}: skill {skill_id} is pinned to `latest`, which moves the deployed body with no change here'
            )
    else:
        directory = _required_str(entry, 'directory', path)
        if pathlib.PurePosixPath(directory).name != directory or directory in ('.', '..'):
            raise ConfigError(
                f'{path}: skill {skill_id}: `directory` is one name under agents/skills/, got {directory!r}'
            )
    return dict(entry)


def read_skill(directory: pathlib.Path) -> SkillFiles:
    """The content of one skill directory, read once: what an upload sends and what its digest is over.

    The file set is git's view of the directory — tracked files and untracked ones the repository's
    ignore rules do not exclude — so an unstaged edit is content and a `__pycache__` is not.

    Raises:
        ConfigError: If `directory` is not a directory inside a git work tree, holds no `SKILL.md` at
            its root, holds a hidden path or a symlink (either would be uploaded as part of the
            skill), or lists a tracked file that is gone from the working tree.
    """
    if not directory.is_dir():
        raise ConfigError(f'{directory} is not a directory')
    listing = subprocess.run(  # noqa: S603 — fixed argv; the path is the caller's
        ['git', '-C', str(directory), 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '.'],  # noqa: S607 — git as installed
        capture_output=True,
        check=False,
    )
    if listing.returncode != 0:
        raise ConfigError(f'{directory}: git cannot list it: {listing.stderr.decode("utf-8", "replace").strip()}')
    files: list[tuple[str, bytes]] = []
    for relative in sorted({entry for entry in listing.stdout.decode('utf-8').split('\0') if entry}):
        path = directory / relative
        if any(part.startswith('.') for part in pathlib.PurePosixPath(relative).parts):
            raise ConfigError(f'{directory}: a hidden path would be uploaded with the skill: {relative}')
        if path.is_symlink():
            raise ConfigError(f'{directory}: a symlink would be uploaded with the skill: {relative}')
        if not path.is_file():
            raise ConfigError(f'{directory}: {relative} is tracked but gone from the working tree; commit the deletion')
        files.append((relative, path.read_bytes()))
    if not any(relative == SKILL_FILE for relative, _ in files):
        raise ConfigError(f'{directory} holds no {SKILL_FILE} at its root')
    return tuple(files)


def head_holding(directory: pathlib.Path, expected: str) -> str:
    """The commit at `HEAD`, provided its tree holds `directory` with digest `expected`.

    The comparison is over bytes rather than `git status`, which user config (`status.showUntrackedFiles`),
    index flags (`--assume-unchanged`, `--skip-worktree`) and a stale fsmonitor can each silence.

    Raises:
        ConfigError: If `HEAD`'s copy of the directory is not the content read from the working tree, or
            git cannot read it.
    """
    head = _git(directory, 'rev-parse', 'HEAD').decode('utf-8').strip()
    files: list[tuple[str, bytes]] = []
    for entry in _git(directory, 'ls-tree', '-r', '-z', head, '--', '.').split(b'\0'):
        if not entry:
            continue
        meta, _, relative = entry.partition(b'\t')
        _mode, kind, obj = meta.decode('utf-8').split(' ')
        if kind != 'blob':
            raise ConfigError(f'{directory}: HEAD holds a {kind} at {relative.decode("utf-8")}, not a file')
        files.append((relative.decode('utf-8'), _git(directory, 'cat-file', 'blob', obj)))
    if digest(tuple(sorted(files))) != expected:
        raise ConfigError(
            f'{directory} differs from what HEAD holds (uncommitted, staged or untracked changes), and a version '
            'published from it would match no commit; commit them first'
        )
    return head


def _git(directory: pathlib.Path, *args: str) -> bytes:
    run = subprocess.run(  # noqa: S603 — fixed argv; the path is the caller's
        ['git', '-C', str(directory), *args],  # noqa: S607 — git as installed
        capture_output=True,
        check=False,
    )
    if run.returncode != 0:
        raise ConfigError(f'{directory}: git {args[0]}: {run.stderr.decode("utf-8", "replace").strip()}')
    return run.stdout


def digest(files: SkillFiles) -> str:
    """`sha256:<hex>` over a skill's files, path and content: what one skill version holds."""
    digester = hashlib.sha256()
    for relative, content in files:
        digester.update(f'{relative}\n{len(content)}\n'.encode())
        digester.update(content)
    return _DIGEST_PREFIX + digester.hexdigest()


def upload_files(directory_name: str, files: SkillFiles) -> list[tuple[str, bytes]]:
    """A skill's files as the API takes them.

    Each lands under the directory's name, which the API reads as the skill's folder and the platform
    lays the skill down as.
    """
    return [(f'{directory_name}/{relative}', content) for relative, content in files]
