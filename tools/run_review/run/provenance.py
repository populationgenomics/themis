"""What a run ran: the agent version its session pinned, every skill version, and the commit each of ours came from.

The session holds a snapshot of the agent taken at creation, with its version, its model settings and
each skill pinned to a version. The snapshot carries no metadata, so the commit a skill was published from is read from
the agent's metadata at that version, where `tools.agents` records it, by the directory each was published
from. Every custom skill is ours: the directories it may come from are the ones the declarations under
`agents/` publish. A skill id differs per workspace, so the record is what ties one to a directory. Nothing the
session or the agent holds names the build of the sandbox worker, the classification library or the
evidence services the run called, so provenance cannot record them.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable, Mapping

from tools.agents import config, control_plane


class ProvenanceError(Exception):
    """The snapshot, the agent's records or the declarations are not in the shape read here, or disagree."""


@dataclasses.dataclass(frozen=True)
class Skill:
    """One skill the session pinned. `directory` and `commit` are set for each of ours, and None for Anthropic's."""

    type: str
    skill_id: str
    version: str
    directory: str | None
    commit: str | None


@dataclasses.dataclass(frozen=True)
class Provenance:
    agent_id: str
    agent_version: int
    # The snapshot's model settings as the API returns them: the model id and what it was run with.
    model: dict[str, object]
    skills: tuple[Skill, ...]


def declared_directories(agents_dir: pathlib.Path = config.AGENTS_DIR) -> frozenset[str]:
    """The skill directories this repository's declarations publish.

    Raises:
        ProvenanceError: If a declaration does not load.
    """
    ours: set[str] = set()
    for path in sorted(agents_dir.glob('*.agent.yaml')):
        try:
            ours.update(config.load(path).skill_directories)
        except config.ConfigError as e:
            raise ProvenanceError(str(e)) from e
    return frozenset(ours)


def read(
    snapshot: Mapping[str, object],
    metadata_at: Callable[[str, int], Mapping[str, str]],
    ours: frozenset[str],
) -> Provenance:
    """The provenance of a session from its agent snapshot and the agent's metadata at the snapshot's version.

    Args:
        snapshot: The session's `agent`, as the API returns it.
        metadata_at: Reads the agent's metadata at an id and a version.
        ours: The skill directories this repository publishes (`declared_directories`).

    Raises:
        ProvenanceError: If the snapshot lacks its id, version or skills, a skill record is malformed, or a custom
            skill has no record, or one naming a directory not of ours or another version.
    """
    agent_id, version, skills = snapshot.get('id'), snapshot.get('version'), snapshot.get('skills')
    model = snapshot.get('model')
    if not isinstance(agent_id, str) or not isinstance(version, int) or not isinstance(skills, list):
        raise ProvenanceError(f'the session agent snapshot names no id, version and skills: {dict(snapshot)}')
    if not isinstance(model, Mapping) or not isinstance(model.get('id'), str):
        raise ProvenanceError(f'the session agent snapshot names no model: {dict(snapshot)}')
    try:
        records = control_plane.skill_records(metadata_at(agent_id, version))
    except control_plane.ControlPlaneError as e:
        raise ProvenanceError(str(e)) from e
    by_id = {record.skill_id: (directory, record) for directory, record in records.items()}
    where = f'{agent_id} v{version}'
    return Provenance(
        agent_id=agent_id,
        agent_version=version,
        model={str(key): value for key, value in model.items()},
        skills=tuple(_skill(entry, by_id, ours, where) for entry in skills),
    )


def _skill(
    entry: object,
    records: Mapping[str, tuple[str, control_plane.SkillRecord]],
    ours: frozenset[str],
    where: str,
) -> Skill:
    if not isinstance(entry, Mapping):
        raise ProvenanceError(f'a pinned skill is not an object: {entry!r}')
    kind, skill_id, version = entry.get('type'), entry.get('skill_id'), entry.get('version')
    if not isinstance(kind, str) or not isinstance(skill_id, str) or not isinstance(version, str):
        raise ProvenanceError(f'a pinned skill names no type, skill_id and version: {dict(entry)}')
    if kind == 'anthropic':
        return Skill(type=kind, skill_id=skill_id, version=version, directory=None, commit=None)
    if kind != 'custom':
        raise ProvenanceError(f'{where} pins {skill_id} of type {kind!r}, neither anthropic nor custom')
    if skill_id not in records:
        raise ProvenanceError(
            f'{where} pins the custom skill {skill_id} and holds no record of it, so no commit can be named for it'
        )
    directory, record = records[skill_id]
    if directory not in ours:
        raise ProvenanceError(
            f'{where} records {skill_id} as published from {directory!r}, which no declaration under agents/ publishes'
        )
    if record.version != version:
        raise ProvenanceError(
            f'the session pinned {skill_id} at {version}, and {where} records {record.version} for it'
        )
    return Skill(type=kind, skill_id=skill_id, version=version, directory=directory, commit=record.commit)


def as_json(provenance: Provenance) -> dict[str, object]:
    return {
        'agent_id': provenance.agent_id,
        'agent_version': provenance.agent_version,
        'model': provenance.model,
        'skills': [dataclasses.asdict(skill) for skill in provenance.skills],
    }
