"""The agent declaration tools: what the yaml is held to, and what apply publishes, pins and records."""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import types
import typing

import anthropic
import pytest
from anthropic.lib import credentials as anthropic_credentials
from anthropic.types.beta import agent_update_params

from tools.agents import __main__ as cli
from tools.agents import config, control_plane

_DECLARATIONS = sorted(config.AGENTS_DIR.glob('*.agent.yaml'))
_BODY = '---\nname: a-skill\ndescription: d\n---\ntext\n'
_MINIMAL = 'name: an agent\n'
_MODEL = 'claude-test-1'
_COMMIT = 'c0ffee' * 6 + 'c0ff'


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git work tree for skill directories: the file set a skill publishes is git's view of its directory."""
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)  # noqa: S603, S607 — a test fixture's git
    (tmp_path / '.gitignore').write_text('__pycache__/\n*.pyc\n', 'utf-8')
    return tmp_path


def _write_skill(directory: pathlib.Path, body: str = _BODY) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / config.SKILL_FILE).write_text(body, 'utf-8')
    return directory


def _declaration(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / 'an.agent.yaml'
    path.write_text(text, 'utf-8')
    return path


def _commit(repo: pathlib.Path) -> str:
    """Commit everything in the work tree, as a reviewed skill reaches `apply`, and return the commit."""
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'add', '-A'], check=True)  # noqa: S603 — a test fixture's git
    subprocess.run([*git, 'commit', '-q', '-m', 'fixture'], check=True)  # noqa: S603 — a test fixture's git
    return subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, check=True, text=True).stdout.strip()  # noqa: S603


# --- the yaml ---


def test_there_is_a_declaration_to_hold() -> None:
    """Parametrising over an empty set skips every case below and reports green."""
    assert _DECLARATIONS, 'no agents/*.agent.yaml found'


@pytest.mark.parametrize('path', _DECLARATIONS, ids=[path.stem for path in _DECLARATIONS])
def test_every_declaration_loads_and_its_directory_skills_are_one_skill_each(path: pathlib.Path) -> None:
    """`read_skill` raises on a directory that is not one skill — no SKILL.md, a hidden path, a symlink."""
    declaration = config.load(path)
    for skill in declaration.directory_skills:
        assert config.read_skill(skill.path)


def test_a_minimal_declaration_loads(tmp_path: pathlib.Path) -> None:
    declaration = config.load(_declaration(tmp_path, _MINIMAL))
    assert (declaration.name, declaration.model) == ('an agent', {})
    assert (declaration.system, declaration.tools, declaration.skills, declaration.multiagent) == (None, [], [], None)


@pytest.mark.parametrize(
    ('text', 'match'),
    [
        ('- not\n- a mapping\n', 'is a mapping'),
        (_MINIMAL + 'mcp_servers: []\n', 'unknown keys'),
        ('model:\n  effort: high\n', '`name`'),
        ('name: x\nmodel: claude-opus-5\n', '`model` is a mapping'),
        ('name: x\nmodel:\n  id: claude-opus-5\n', 'confidential stack config'),
        (_MINIMAL + 'system: 3\n', '`system`'),
        (_MINIMAL + 'tools: {}\n', '`tools`'),
        (_MINIMAL + 'skills:\n  - type: other\n    skill_id: s\n    version: "1"\n', 'skill `type`'),
        (_MINIMAL + 'skills:\n  - type: anthropic\n    skill_id: pdf\n', 'exactly one of a `version`'),
        (_MINIMAL + 'skills:\n  - type: anthropic\n    skill_id: pdf\n    directory: d\n', 'unknown keys'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n', 'exactly one of a `version` or a `directory`'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    version: "1"\n    directory: d\n', 'exactly one'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    version: 1788413284061808\n', 'quote it'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    version: ""\n', '`version`'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    version: latest\n', '`latest`'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    directory: ""\n', '`directory`'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    directory: a/b\n', 'one name under'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    directory: /tmp/a\n', 'one name under'),
        (_MINIMAL + 'skills:\n  - type: custom\n    skill_id: s\n    directory: ..\n', 'one name under'),
    ],
    ids=[
        'not-a-mapping',
        'unknown-key',
        'no-name',
        'model-as-a-bare-id',
        'model-with-an-id',
        'system-not-a-string',
        'tools-not-a-list',
        'skill-type',
        'anthropic-skill-unpinned',
        'anthropic-skill-with-directory',
        'custom-skill-unpinned',
        'custom-skill-pinned-twice',
        'version-as-integer',
        'version-empty',
        'version-latest',
        'directory-empty',
        'directory-nested',
        'directory-absolute',
        'directory-parent',
    ],
)
def test_a_declaration_outside_the_shape_is_refused(tmp_path: pathlib.Path, text: str, match: str) -> None:
    with pytest.raises(config.ConfigError, match=match):
        config.load(_declaration(tmp_path, text))


def test_directory_skills_are_the_custom_entries_pinned_by_directory(tmp_path: pathlib.Path) -> None:
    text = _MINIMAL + (
        'skills:\n'
        '  - type: anthropic\n    skill_id: pdf\n    version: "20260709"\n'
        '  - type: custom\n    skill_id: skill_a\n    directory: a\n'
        '  - type: custom\n    skill_id: skill_b\n    version: "7"\n'
    )
    declaration = config.load(_declaration(tmp_path, text))
    assert declaration.directory_skills == (config.DirectorySkill(skill_id='skill_a', directory='a'),)


# --- a skill directory's content ---


def test_a_skill_is_read_as_git_sees_its_directory(repo: pathlib.Path) -> None:
    """Tracked and untracked files are content; what the repository ignores is not, staged or not."""
    skill = _write_skill(repo / 'my-skill')
    (skill / 'scripts').mkdir()
    (skill / 'scripts' / 'a.py').write_text('print(1)\n', 'utf-8')
    (skill / 'scripts' / '__pycache__').mkdir()
    (skill / 'scripts' / '__pycache__' / 'a.cpython-313.pyc').write_bytes(b'\x00')
    subprocess.run(['git', '-C', str(repo), 'add', 'my-skill/SKILL.md'], check=True)  # noqa: S603, S607

    files = config.read_skill(skill)

    assert [relative for relative, _ in files] == ['SKILL.md', 'scripts/a.py']
    assert config.upload_files('my-skill', files) == [
        ('my-skill/SKILL.md', _BODY.encode()),
        ('my-skill/scripts/a.py', b'print(1)\n'),
    ]


def test_the_digest_reads_the_files_by_path_and_content(repo: pathlib.Path) -> None:
    first = _write_skill(repo / 'first')
    (first / 'a.py').write_text('print(1)\n', 'utf-8')
    second = _write_skill(repo / 'second')
    (second / 'a.py').write_text('print(1)\n', 'utf-8')
    assert config.digest(config.read_skill(first)) == config.digest(config.read_skill(second))  # the name is not in it
    (second / 'a.py').rename(second / 'b.py')
    assert config.digest(config.read_skill(first)) != config.digest(config.read_skill(second))  # a path is
    (second / 'b.py').rename(second / 'a.py')
    (second / 'a.py').write_text('print(2)\n', 'utf-8')
    assert config.digest(config.read_skill(first)) != config.digest(config.read_skill(second))  # and so is content


def _tracked_then_deleted(directory: pathlib.Path) -> None:
    _write_skill(directory)
    (directory / 'gone.md').write_text('x\n', 'utf-8')
    subprocess.run(['git', '-C', str(directory), 'add', 'gone.md'], check=True)  # noqa: S603, S607
    (directory / 'gone.md').unlink()


@pytest.mark.parametrize(
    ('prepare', 'match'),
    [
        (lambda _d: None, 'not a directory'),
        (lambda d: d.mkdir(), f'no {config.SKILL_FILE}'),
        (lambda d: (_write_skill(d) / '.env').write_bytes(b''), 'hidden path'),
        (lambda d: (_write_skill(d) / 'link.md').symlink_to(d / config.SKILL_FILE), 'symlink'),
        (_tracked_then_deleted, 'gone from the working tree'),
    ],
    ids=['missing', 'no-skill-file', 'hidden-file', 'symlink', 'tracked-but-deleted'],
)
def test_a_directory_that_is_not_one_skill_is_refused(
    repo: pathlib.Path, prepare: typing.Callable[[pathlib.Path], object], match: str
) -> None:
    directory = repo / 'skill'
    prepare(directory)
    with pytest.raises(config.ConfigError, match=match):
        config.read_skill(directory)


def test_a_directory_outside_a_git_work_tree_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(config.ConfigError, match='git cannot list'):
        config.read_skill(_write_skill(tmp_path / 'skill'))


# --- the record on the agent ---


def test_the_declaration_carries_every_field_an_update_can_set() -> None:
    """A settable field left out of `AgentParams` would survive every apply unchanged, and `diff` would not show it.

    `betas` is a request flag and `version` the concurrency token, so neither is agent state.
    """
    settable = set(typing.get_type_hints(agent_update_params.AgentUpdateParams)) - {'betas', 'version'}
    assert {field.name for field in dataclasses.fields(control_plane.AgentParams)} == settable


def test_a_record_round_trips_through_the_metadata_value() -> None:
    record = control_plane.SkillRecord(
        skill_id='skill_a', version='1788933683103578', digest='sha256:ab', commit=_COMMIT
    )
    assert control_plane.SkillRecord.parse('skill:a', record.format()) == record


@pytest.mark.parametrize(
    'value',
    [
        '',
        'skill_a',
        'skill_a@1',
        f'skill_a@1 md5:x {_COMMIT}',
        f'@1 sha256:ab {_COMMIT}',
        f'skill_a@ sha256:ab {_COMMIT}',
        'skill_a@1 sha256:ab',
        'skill_a@1 sha256:ab main',
        f'skill_a@1 sha256:ab {_COMMIT} more',
    ],
)
def test_a_metadata_value_that_is_not_a_record_is_refused(value: str) -> None:
    with pytest.raises(control_plane.ControlPlaneError, match='not a skill record'):
        control_plane.SkillRecord.parse('skill:a', value)


# --- the calls, against a fake control plane ---


class _FakeVersions:
    def __init__(self) -> None:
        self.created: list[tuple[str, list[tuple[str, bytes]]]] = []

    def create(self, skill_id: str, *, files: list[tuple[str, bytes]]) -> types.SimpleNamespace:
        self.created.append((skill_id, files))
        return types.SimpleNamespace(
            id=f'skill_version_{len(self.created)}',
            version=str(1_000_000 + len(self.created)),
            directory=files[0][0].split('/')[0],
        )


class _FakeSkills:
    def __init__(self, versions: _FakeVersions) -> None:
        self.versions = versions
        self.created: list[tuple[list[tuple[str, bytes]], str | None]] = []

    def create(self, *, files: list[tuple[str, bytes]], display_title: str | None = None) -> types.SimpleNamespace:
        self.created.append((files, display_title))
        first = self.versions.create(f'skill_{len(self.created)}', files=files)
        return types.SimpleNamespace(id=f'skill_{len(self.created)}', latest_version=first.version)


class _FakeAgents:
    """Holds one agent as the fields sent, versioned as the API versions: a changing update bumps it.

    `metadata` merges per key as the API does — a string upserts, `None` deletes. `model_dump` answers as the
    server does: a roster `self` entry resolved to the agent's id and version, a bare `effort` expanded to an
    object, a default `speed` and permission policy filled in.
    """

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, object]] = {}
        self.versions: dict[str, int] = {}
        self.sent: list[set[str]] = []  # the field names of each create and update, in order

    def create(self, **fields: object) -> types.SimpleNamespace:
        self.sent.append(set(fields))
        agent_id = f'agent_{len(self.agents) + 1}'
        self.agents[agent_id] = fields
        self.versions[agent_id] = 1
        return self._agent(agent_id)

    def retrieve(self, agent_id: str) -> types.SimpleNamespace:
        return self._agent(agent_id)

    def update(self, agent_id: str, **fields: object) -> types.SimpleNamespace:
        self.sent.append(set(fields))
        current = dict(self.agents[agent_id])
        patch = typing.cast('dict[str, str | None]', fields.pop('metadata'))
        metadata = {**typing.cast('dict[str, str]', current.get('metadata', {}))}
        for key, value in patch.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        merged = {**fields, 'metadata': metadata}
        if current != merged:
            self.agents[agent_id] = merged
            self.versions[agent_id] += 1
        return self._agent(agent_id)

    def _agent(self, agent_id: str) -> types.SimpleNamespace:
        version = self.versions[agent_id]
        return types.SimpleNamespace(
            id=agent_id,
            version=version,
            metadata=dict(typing.cast('dict[str, str]', self.agents[agent_id].get('metadata', {}))),
            model_dump=lambda: self._server_view(agent_id, version),
        )

    def _server_view(self, agent_id: str, version: int) -> dict[str, object]:
        fields = dict(self.agents[agent_id])
        model = fields['model']
        if isinstance(model, dict):
            effort = model.get('effort')
            fields['model'] = {
                **model,
                'effort': {'type': effort} if isinstance(effort, str) else effort,
                'speed': 'standard',
                'inference_geo': None,
            }
        multiagent = fields.get('multiagent')
        if isinstance(multiagent, dict):
            fields['multiagent'] = {
                **multiagent,
                'agents': [
                    {'type': 'agent', 'id': agent_id, 'version': version} if entry == {'type': 'self'} else entry
                    for entry in typing.cast('list[dict[str, object]]', multiagent['agents'])
                ],
            }
        fields['tools'] = [
            _with_policy(tool) for tool in typing.cast('list[dict[str, object]]', fields.get('tools', []))
        ]
        return {**fields, 'id': agent_id, 'version': version}


def _with_policy(tool: dict[str, object]) -> dict[str, object]:
    if 'default_config' not in tool:
        return tool
    default_config = typing.cast('dict[str, object]', tool['default_config'])
    return {**tool, 'default_config': {**default_config, 'permission_policy': {'type': 'always_allow'}}}


def _fake_client() -> tuple[anthropic.Anthropic, _FakeAgents, _FakeSkills]:
    agents, skills = _FakeAgents(), _FakeSkills(_FakeVersions())
    beta = types.SimpleNamespace(agents=agents, skills=skills)
    client = types.SimpleNamespace(beta=beta, api_key=None, auth_token='t', credentials=None)
    return typing.cast('anthropic.Anthropic', client), agents, skills


def _declared(monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path) -> config.AgentConfig:
    monkeypatch.setattr(config, 'SKILLS_DIR', repo / 'skills')
    _write_skill(repo / 'skills' / 'a')
    text = _MINIMAL + (
        'model:\n  effort: xhigh\n'
        'system: use the skill\n'
        'tools:\n  - type: agent_toolset_20260401\n    default_config:\n      enabled: true\n'
        'skills:\n'
        '  - type: anthropic\n    skill_id: pdf\n    version: "20260709"\n'
        '  - type: custom\n    skill_id: skill_a\n    directory: a\n'
        'multiagent:\n  type: coordinator\n  agents:\n    - type: self\n'
    )
    declaration = config.load(_declaration(repo, text))
    _commit(repo)
    return declaration


def _record(agents: _FakeAgents, agent_id: str, directory: str) -> control_plane.SkillRecord:
    metadata = typing.cast('dict[str, str]', agents.agents[agent_id]['metadata'])
    return control_plane.SkillRecord.parse(f'skill:{directory}', metadata[f'skill:{directory}'])


def test_create_and_apply_send_every_field_the_declaration_carries(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    """The requests name their fields one by one, so a field `AgentParams` gains is sent only if both calls pass it."""
    declaration = _declared(monkeypatch, repo)
    client, agents, _ = _fake_client()
    carried = {field.name for field in dataclasses.fields(control_plane.AgentParams)}

    created = control_plane.create(declaration, _MODEL, client)
    control_plane.apply(declaration, _MODEL, client, created.id)

    assert agents.sent == [carried, carried]


def test_create_publishes_every_directory_skill_and_records_it(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, agents, skills = _fake_client()

    created = control_plane.create(declaration, _MODEL, client)

    assert skills.versions.created[0][0] == 'skill_a'
    assert [name for name, _ in skills.versions.created[0][1]] == ['a/SKILL.md']
    version = skills.versions.created[0] and '1000001'
    assert agents.agents[created.id]['skills'] == [
        {'type': 'anthropic', 'skill_id': 'pdf', 'version': '20260709'},
        {'type': 'custom', 'skill_id': 'skill_a', 'version': version},
    ]
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, check=True, text=True)  # noqa: S603, S607
    assert _record(agents, created.id, 'a') == control_plane.SkillRecord(
        skill_id='skill_a',
        version=version,
        digest=config.digest(config.read_skill(config.SKILLS_DIR / 'a')),
        commit=head.stdout.strip(),
    )
    assert agents.agents[created.id]['mcp_servers'] == []
    assert agents.agents[created.id]['model'] == {'id': _MODEL, 'effort': 'xhigh'}
    assert agents.agents[created.id]['description'] is None  # absent from the yaml: cleared on the agent, not preserved


def test_apply_publishes_nothing_and_leaves_the_version_when_nothing_moved(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, _, skills = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)

    applied = control_plane.apply(declaration, _MODEL, client, created.id)

    assert applied.published == ()
    assert (applied.before, applied.after) == (1, 1)
    assert len(skills.versions.created) == 1


def test_apply_publishes_a_directory_whose_content_moved_and_pins_the_new_version(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, agents, skills = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    (config.SKILLS_DIR / 'a' / config.SKILL_FILE).write_text('---\nname: a-skill\ndescription: d\n---\nmore\n', 'utf-8')
    head = _commit(repo)

    applied = control_plane.apply(declaration, _MODEL, client, created.id)

    assert [item.directory for item in applied.published] == ['a']
    assert _record(agents, created.id, 'a').commit == head
    assert (applied.before, applied.after) == (1, 2)
    assert len(skills.versions.created) == 2
    skills_sent = typing.cast('list[dict[str, object]]', agents.agents[created.id]['skills'])
    assert skills_sent[1] == {'type': 'custom', 'skill_id': 'skill_a', 'version': '1000002'}
    assert _record(agents, created.id, 'a').version == '1000002'
    # And the moved content is now what the agent runs, so a second apply publishes nothing.
    again = control_plane.apply(declaration, _MODEL, client, created.id)
    assert again.published == ()
    assert (again.before, again.after) == (2, 2)


def _edit(skill: pathlib.Path) -> None:
    (skill / config.SKILL_FILE).write_text('---\nname: a-skill\ndescription: d\n---\nedit\n', 'utf-8')


def _add_untracked(skill: pathlib.Path) -> None:
    (skill / 'notes.md').write_text('a scratch note\n', 'utf-8')


def _stage(skill: pathlib.Path) -> None:
    (skill / 'staged.md').write_text('staged, not committed\n', 'utf-8')
    subprocess.run(['git', '-C', str(skill), 'add', 'staged.md'], check=True)  # noqa: S603, S607 — a test fixture's git


def _add_untracked_status_hides(skill: pathlib.Path) -> None:
    """An untracked file under a config that keeps `git status` from listing it."""
    subprocess.run(['git', '-C', str(skill), 'config', 'status.showUntrackedFiles', 'no'], check=True)  # noqa: S603, S607
    _add_untracked(skill)


def _edit_assumed_unchanged(skill: pathlib.Path) -> None:
    """An edit to a file the index is told to assume unchanged, which `git status` then does not report."""
    subprocess.run(['git', '-C', str(skill), 'update-index', '--assume-unchanged', config.SKILL_FILE], check=True)  # noqa: S603, S607
    _edit(skill)


@pytest.mark.parametrize(
    'change',
    [_edit, _add_untracked, _stage, _add_untracked_status_hides, _edit_assumed_unchanged],
    ids=['uncommitted-edit', 'untracked-file', 'staged-file', 'untracked-status-hides', 'assumed-unchanged'],
)
def test_apply_refuses_a_skill_directory_no_commit_holds_before_uploading_anything(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path, change: typing.Callable[[pathlib.Path], object]
) -> None:
    """A version published from the working tree would pin the agent to content no commit holds."""
    declaration = _declared(monkeypatch, repo)
    client, agents, skills = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    change(config.SKILLS_DIR / 'a')

    with pytest.raises(config.ConfigError, match='commit them first'):
        control_plane.apply(declaration, _MODEL, client, created.id)

    assert len(skills.versions.created) == 1
    assert agents.versions[created.id] == 1


def test_a_committed_directory_publishes_with_an_ignored_file_beside_it(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, agents, _ = _fake_client()
    cache = config.SKILLS_DIR / 'a' / '__pycache__'
    cache.mkdir()
    (cache / 'x.cpython-313.pyc').write_bytes(b'\x00')

    created = control_plane.create(declaration, _MODEL, client)

    assert _record(agents, created.id, 'a').commit != ''


def test_one_dirty_directory_stops_every_upload(monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path) -> None:
    """The check runs for every pending directory before the first upload, not beside each one."""
    monkeypatch.setattr(config, 'SKILLS_DIR', repo / 'skills')
    _write_skill(repo / 'skills' / 'a')
    _write_skill(repo / 'skills' / 'b')
    text = _MINIMAL + (
        'skills:\n'
        '  - type: custom\n    skill_id: skill_a\n    directory: a\n'
        '  - type: custom\n    skill_id: skill_b\n    directory: b\n'
    )
    declaration = config.load(_declaration(repo, text))
    _commit(repo)
    _add_untracked(repo / 'skills' / 'b')
    client, _, skills = _fake_client()

    with pytest.raises(config.ConfigError, match='commit them first'):
        control_plane.create(declaration, _MODEL, client)

    assert skills.versions.created == []


def test_apply_publishes_again_when_the_record_names_another_skill(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    """The yaml moved the directory to a new skill container; the version recorded belongs to the old one."""
    declaration = _declared(monkeypatch, repo)
    client, agents, skills = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    metadata = typing.cast('dict[str, str]', agents.agents[created.id]['metadata'])
    metadata['skill:a'] = metadata['skill:a'].replace('skill_a@', 'skill_old@')

    applied = control_plane.apply(declaration, _MODEL, client, created.id)

    assert len(applied.published) == 1
    assert skills.versions.created[-1][0] == 'skill_a'


def test_apply_deletes_the_record_of_a_directory_the_declaration_dropped(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, agents, _ = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    metadata = typing.cast('dict[str, str]', agents.agents[created.id]['metadata'])
    metadata['skill:dropped'] = f'skill_x@1 sha256:ab {_COMMIT}'
    metadata['owner'] = 'kept'  # a key that is not a skill record is not the declaration's to touch

    control_plane.apply(declaration, _MODEL, client, created.id)

    assert set(typing.cast('dict[str, str]', agents.agents[created.id]['metadata'])) == {'skill:a', 'owner'}


def test_apply_refuses_a_record_it_cannot_read(monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path) -> None:
    declaration = _declared(monkeypatch, repo)
    client, agents, _ = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    typing.cast('dict[str, str]', agents.agents[created.id]['metadata'])['skill:a'] = 'edited by hand'
    with pytest.raises(control_plane.ControlPlaneError, match='not a skill record'):
        control_plane.apply(declaration, _MODEL, client, created.id)


def test_publish_refuses_a_version_filed_under_another_folder(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, _, skills = _fake_client()
    monkeypatch.setattr(
        skills.versions,
        'create',
        lambda _skill_id, *, files: types.SimpleNamespace(id='v', version='1', directory='elsewhere', files=files),
    )
    with pytest.raises(control_plane.ControlPlaneError, match="'elsewhere'"):
        control_plane.create(declaration, _MODEL, client)


def test_create_skill_uploads_the_directory_as_the_first_version_and_names_the_skill(repo: pathlib.Path) -> None:
    directory = _write_skill(repo / 'new-skill')
    client, _, skills = _fake_client()
    skill_id = control_plane.create_skill(directory, client)
    assert skill_id == 'skill_1'
    assert skills.created[0][1] == 'new-skill'
    assert [name for name, _ in skills.created[0][0]] == ['new-skill/SKILL.md']


def test_diff_is_empty_across_the_servers_defaults_and_expansions(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    """The server's view carries what the declaration does not say; none of it is a difference."""
    declaration = _declared(monkeypatch, repo)
    client, _, _ = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    assert control_plane.diff(declaration, _MODEL, client, created.id) == ''


def test_diff_reports_a_moved_skill_as_unpublished_and_a_changed_field(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, agents, _ = _fake_client()
    created = control_plane.create(declaration, _MODEL, client)
    agents.agents[created.id]['system'] = 'an older prompt'
    extra = {'type': 'custom', 'skill_id': 'skill_extra', 'version': '9'}
    agents.agents[created.id]['skills'] = [*typing.cast('list[object]', agents.agents[created.id]['skills']), extra]
    (config.SKILLS_DIR / 'a' / config.SKILL_FILE).write_text('---\nname: a-skill\ndescription: d\n---\nmore\n', 'utf-8')

    report = control_plane.diff(declaration, _MODEL, client, created.id)

    assert '-  "system": "an older prompt"' in report
    assert '+  "system": "use the skill"' in report
    assert 'skill_extra' in report  # a skill the agent carries beyond the declaration is a difference
    assert '<unpublished; apply publishes it>' in report  # the moved directory, read-only, so not published here


# --- the command ---


def test_the_command_refuses_to_run_without_the_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    client, _, _ = _fake_client()
    monkeypatch.setattr(anthropic, 'Anthropic', lambda: client)
    monkeypatch.delenv(config.MODEL_ID_ENV, raising=False)
    with pytest.raises(SystemExit, match=f'{config.MODEL_ID_ENV} is unset'):
        cli.main(['create', str(_declaration(tmp_path, _MINIMAL))])


def test_the_command_refuses_to_run_with_no_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(
        anthropic, 'Anthropic', lambda: types.SimpleNamespace(api_key=None, auth_token=None, credentials=None)
    )
    with pytest.raises(SystemExit, match='no Anthropic credentials'):
        cli.main(['apply', str(_declaration(tmp_path, _MINIMAL)), '--agent-id', 'agent_1'])


def test_the_command_refuses_an_empty_agent_id(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SystemExit, match='--agent-id is empty'):
        cli.main(['apply', str(_declaration(tmp_path, _MINIMAL)), '--agent-id', ''])


def test_a_failed_token_exchange_names_the_rule_and_account_it_ran_under(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A placeholder rule id on a fresh environment fails here; the message says which ids were presented."""

    def refused(_agent_id: str) -> typing.NoReturn:
        raise anthropic_credentials.WorkloadIdentityError('invalid_grant', status_code=400, body={}, request_id=None)

    client, agents, _ = _fake_client()
    monkeypatch.setattr(agents, 'retrieve', refused)
    monkeypatch.setattr(anthropic, 'Anthropic', lambda: client)
    monkeypatch.setenv(config.MODEL_ID_ENV, _MODEL)
    monkeypatch.setenv('ANTHROPIC_FEDERATION_RULE_ID', 'pending-registration')
    monkeypatch.setenv('ANTHROPIC_SERVICE_ACCOUNT_ID', 'pending-registration')
    with pytest.raises(SystemExit, match=r"rule 'pending-registration' for service account 'pending-registration'"):
        cli.main(['apply', str(_declaration(tmp_path, _MINIMAL)), '--agent-id', 'agent_1'])


def test_the_command_creates_applies_and_reports(
    monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    declaration = _declared(monkeypatch, repo)
    client, _, _ = _fake_client()
    monkeypatch.setattr(anthropic, 'Anthropic', lambda: client)
    monkeypatch.setenv(config.MODEL_ID_ENV, _MODEL)

    assert cli.main(['create', str(declaration.path)]) == 0
    agent_id = capsys.readouterr().out.strip()
    assert cli.main(['apply', str(declaration.path), '--agent-id', agent_id]) == 0
    assert capsys.readouterr().out.strip() == f'{agent_id}: unchanged at v1'
    (config.SKILLS_DIR / 'a' / config.SKILL_FILE).write_text('---\nname: a-skill\ndescription: d\n---\nmore\n', 'utf-8')
    with pytest.raises(SystemExit, match='commit them first'):
        cli.main(['apply', str(declaration.path), '--agent-id', agent_id])
    _commit(repo)
    assert cli.main(['apply', str(declaration.path), '--agent-id', agent_id]) == 0
    assert capsys.readouterr().out.splitlines() == [
        'published agents/skills/a as version 1000002',
        f'{agent_id}: v1 -> v2',
    ]
    assert cli.main(['diff', str(declaration.path), '--agent-id', agent_id]) == 0
    assert capsys.readouterr().out.strip() == 'no difference over the declared fields'


def test_the_command_exits_naming_a_declaration_fault(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    client, _, _ = _fake_client()
    monkeypatch.setattr(anthropic, 'Anthropic', lambda: client)
    monkeypatch.setenv(config.MODEL_ID_ENV, _MODEL)
    with pytest.raises(SystemExit, match='`name`'):
        cli.main(['create', str(_declaration(tmp_path, 'model: {}\n'))])
