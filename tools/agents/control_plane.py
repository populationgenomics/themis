"""The control-plane calls: publish the skills a declaration holds, create or update the agent, read it back for a diff.

The agent carries the record of what was published for it. Each directory skill's record sits in the
agent's `metadata` under `skill:<directory>`, as `<skill_id>@<version> <digest> <commit>`: the version
the agent is pinned to, the digest of the content it was published from, and the commit that holds that
content. An apply publishes a new version only where the digest is not the recorded one, so a deploy
that changed no skill leaves the skill and the agent's version alone. It publishes only a directory a
commit holds, so every record names content git can reproduce. The API cannot say what a published
version holds, which is why the agent has to.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import pathlib
import re
import typing
from collections.abc import Mapping, Sequence

import anthropic
from anthropic.types import beta as beta_types
from anthropic.types.beta import agent_create_params

from tools.agents import config as config_mod

_SELF = {'type': 'self'}
_RECORD_PREFIX = 'skill:'
_UNPUBLISHED = '<unpublished; apply publishes it>'
_COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


class ControlPlaneError(RuntimeError):
    """The API answered with something the declaration did not ask for, or the agent's record is not readable."""


@dataclasses.dataclass(frozen=True)
class SkillRecord:
    """What the agent runs for one directory: the version pinned, its content's digest, and the commit holding it."""

    skill_id: str
    version: str
    digest: str
    commit: str

    @classmethod
    def parse(cls, key: str, value: str) -> SkillRecord:
        parts = value.split(' ')
        if len(parts) != 3:
            raise ControlPlaneError(f'the agent metadata {key!r} is not a skill record: {value!r}')
        head, digest, commit = parts
        skill_id, at, version = head.partition('@')
        if not (skill_id and at and version and digest.startswith('sha256:') and _COMMIT.fullmatch(commit)):
            raise ControlPlaneError(f'the agent metadata {key!r} is not a skill record: {value!r}')
        return cls(skill_id=skill_id, version=version, digest=digest, commit=commit)

    def format(self) -> str:
        return f'{self.skill_id}@{self.version} {self.digest} {self.commit}'


def record_key(directory: str) -> str:
    return f'{_RECORD_PREFIX}{directory}'


@dataclasses.dataclass(frozen=True)
class AgentParams:
    """The declaration as one `create` or `update` sends it: every field an update can set, so the agent is the yaml.

    The yaml's structures are handed to the API as written and validated there; the casts name the
    shapes the SDK types them as. `mcp_servers` is always empty: the agent reaches every service in
    code mode, and the field is sent so a server attached elsewhere does not survive an apply.
    `metadata` carries the skill records, and `None` for a record whose directory the declaration no
    longer holds, which the API reads as a deletion.
    """

    name: str
    model: agent_create_params.Model
    system: str | None
    description: str | None
    tools: list[agent_create_params.Tool]
    skills: list[beta_types.BetaManagedAgentsSkillParams]
    multiagent: beta_types.BetaManagedAgentsMultiagentParams | None
    metadata: dict[str, str | None]
    mcp_servers: list[beta_types.BetaManagedAgentsURLMCPServerParams] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Pending:
    """A directory skill whose tracked content is not what the agent runs, read once for upload and digest."""

    skill: config_mod.DirectorySkill
    files: config_mod.SkillFiles
    digest: str


@dataclasses.dataclass(frozen=True)
class Resolution:
    """The declaration against an agent's records: what is pinned already, what has to be published first."""

    records: dict[str, SkillRecord]
    pending: tuple[Pending, ...]
    stale_keys: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Published:
    directory: str
    version: str


@dataclasses.dataclass(frozen=True)
class Applied:
    """The agent's version before and after an `apply`, and the skills published on the way."""

    agent_id: str
    before: int
    after: int
    published: tuple[Published, ...]


def resolve(config: config_mod.AgentConfig, metadata: Mapping[str, str]) -> Resolution:
    """Read every directory skill and settle it against the agent's records.

    A directory whose record names its skill and holds its digest keeps its recorded version; any
    other is pending. A record for a directory the declaration no longer holds is stale.

    Raises:
        ControlPlaneError: If a record under `skill:` is not in the record's shape.
        config_mod.ConfigError: If a directory is not one skill.
    """
    records: dict[str, SkillRecord] = {}
    pending: list[Pending] = []
    for skill in config.directory_skills:
        files = config_mod.read_skill(skill.path)
        digest = config_mod.digest(files)
        key = record_key(skill.directory)
        record = SkillRecord.parse(key, metadata[key]) if key in metadata else None
        if record is not None and record.skill_id == skill.skill_id and record.digest == digest:
            records[skill.directory] = record
        else:
            pending.append(Pending(skill=skill, files=files, digest=digest))
    declared = {skill.directory for skill in config.directory_skills}
    stale = tuple(
        sorted(
            key
            for key in metadata
            if key.startswith(_RECORD_PREFIX) and key.removeprefix(_RECORD_PREFIX) not in declared
        )
    )
    return Resolution(records=records, pending=tuple(pending), stale_keys=stale)


def publish(resolution: Resolution, client: anthropic.Anthropic) -> tuple[dict[str, SkillRecord], list[Published]]:
    """Upload every pending directory as a new version of its skill, each recorded with the commit holding it.

    Returns:
        The records, the recorded ones and the new, and what was uploaded.

    Raises:
        config_mod.ConfigError: If a pending directory has uncommitted or untracked changes; raised
            before anything is uploaded.
        ControlPlaneError: If the API filed a version under a folder other than the directory's name,
            which is the path the skill's own text and the platform's layout rely on.
    """
    commits = {
        item.skill.directory: config_mod.head_holding(item.skill.path, item.digest) for item in resolution.pending
    }
    records = dict(resolution.records)
    published: list[Published] = []
    for item in resolution.pending:
        skill = item.skill
        version = client.beta.skills.versions.create(
            skill.skill_id, files=config_mod.upload_files(skill.directory, item.files)
        )
        if version.directory != skill.directory:
            raise ControlPlaneError(
                f'{skill.skill_id}: version {version.version} was filed under {version.directory!r}, '
                f'not {skill.directory!r}'
            )
        records[skill.directory] = SkillRecord(
            skill_id=skill.skill_id, version=version.version, digest=item.digest, commit=commits[skill.directory]
        )
        published.append(Published(directory=skill.directory, version=version.version))
    return records, published


def agent_params(
    config: config_mod.AgentConfig,
    model_id: str,
    records: Mapping[str, SkillRecord],
    stale_keys: Sequence[str] = (),
) -> AgentParams:
    """The declaration on `model_id`, every directory skill pinned to its record.

    A directory skill without a record reads as unpublished, in the pin and in its metadata value.
    """
    skills: list[dict[str, object]] = []
    metadata: dict[str, str | None] = dict.fromkeys(stale_keys)
    for entry in config.skills:
        if 'directory' in entry:
            directory = str(entry['directory'])
            record = records.get(directory)
            skills.append(
                {
                    'type': 'custom',
                    'skill_id': entry['skill_id'],
                    'version': record.version if record is not None else _UNPUBLISHED,
                }
            )
            metadata[record_key(directory)] = record.format() if record is not None else _UNPUBLISHED
        else:
            skills.append(dict(entry))
    return AgentParams(
        name=config.name,
        model=typing.cast('agent_create_params.Model', {'id': model_id, **config.model}),
        system=config.system,
        description=config.description,
        tools=typing.cast('list[agent_create_params.Tool]', config.tools),
        skills=typing.cast('list[beta_types.BetaManagedAgentsSkillParams]', skills),
        multiagent=typing.cast('beta_types.BetaManagedAgentsMultiagentParams | None', config.multiagent),
        metadata=metadata,
    )


def create(
    config: config_mod.AgentConfig, model_id: str, client: anthropic.Anthropic
) -> beta_types.BetaManagedAgentsAgent:
    """Publish every directory skill, then create the agent the declaration describes, its records with it."""
    records, _ = publish(resolve(config, {}), client)
    params = agent_params(config, model_id, records)
    return client.beta.agents.create(
        name=params.name,
        model=params.model,
        system=params.system,
        description=params.description,
        tools=params.tools,
        skills=params.skills,
        multiagent=params.multiagent,
        metadata=typing.cast('dict[str, str]', params.metadata),
        mcp_servers=params.mcp_servers,
    )


def apply(config: config_mod.AgentConfig, model_id: str, client: anthropic.Anthropic, agent_id: str) -> Applied:
    """Publish what moved, then update `agent_id` to the declaration, whole.

    Sent without a `version`, the update is last-write-wins — the declaration is the truth, whatever
    the agent held — and the API creates a new agent version only when a field changed.
    """
    before = client.beta.agents.retrieve(agent_id)
    resolution = resolve(config, before.metadata)
    records, published = publish(resolution, client)
    params = agent_params(config, model_id, records, resolution.stale_keys)
    after = client.beta.agents.update(
        agent_id,
        name=params.name,
        model=params.model,
        system=params.system,
        description=params.description,
        tools=params.tools,
        skills=params.skills,
        multiagent=params.multiagent,
        metadata=params.metadata,
        mcp_servers=params.mcp_servers,
    )
    return Applied(agent_id=agent_id, before=before.version, after=after.version, published=tuple(published))


def create_skill(directory: pathlib.Path, client: anthropic.Anthropic) -> str:
    """Create a skill from `directory`, its content the first version, and return its id for the declaration."""
    files = config_mod.read_skill(directory)
    skill = client.beta.skills.create(
        files=config_mod.upload_files(directory.name, files), display_title=directory.name
    )
    return skill.id


def diff(config: config_mod.AgentConfig, model_id: str, client: anthropic.Anthropic, agent_id: str) -> str:
    """A unified diff from the agent as deployed to the declaration, over what the declaration says. Read-only.

    A directory skill whose content the agent does not run yet shows as unpublished. The deployed
    agent is projected onto the declaration's shape before comparing: a key the declaration does not
    set — a tool config's permission policy, a model's speed — is dropped, a shorthand the API
    expanded (`effort: xhigh` stored as `{type: xhigh}`) is folded back, and a roster `self` entry the
    API resolved to this agent's id reads as `self` again. A list item the agent has beyond the
    declaration stays, since that is a difference. Empty means no difference.
    """
    deployed = client.beta.agents.retrieve(agent_id)
    resolution = resolve(config, deployed.metadata)
    desired = agent_params(config, model_id, resolution.records, resolution.stale_keys).as_dict()
    projected = _project(deployed.model_dump(), desired, agent_id)
    return ''.join(
        difflib.unified_diff(
            _lines(projected), _lines(desired), fromfile=f'{agent_id} v{deployed.version}', tofile='declaration'
        )
    )


def _project(deployed: object, declared: object, agent_id: str) -> object:
    if isinstance(declared, dict) and isinstance(deployed, dict):
        if declared == _SELF and deployed.get('type') == 'agent' and deployed.get('id') == agent_id:
            return dict(_SELF)
        return {key: _project(deployed.get(key), value, agent_id) for key, value in declared.items()}
    if isinstance(declared, list) and isinstance(deployed, list):
        aligned = [_project(item, wanted, agent_id) for item, wanted in zip(deployed, declared, strict=False)]
        return [*aligned, *deployed[len(declared) :]]
    if isinstance(declared, str) and isinstance(deployed, dict) and deployed == {'type': declared}:
        return declared
    return deployed


def _lines(document: object) -> Sequence[str]:
    return json.dumps(document, indent=2, sort_keys=True, default=str).splitlines(keepends=True)
