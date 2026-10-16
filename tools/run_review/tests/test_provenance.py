from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping

import pytest

from tools.run_review.run import provenance

COMMIT = 'c' * 40
DIGEST = 'sha256:' + 'd' * 64
OURS = {'skill_ours': 'classifying'}
SNAPSHOT = {
    'id': 'agent_1',
    'version': 36,
    'model': {'id': 'claude-model', 'effort': 'high'},
    'skills': [
        {'type': 'anthropic', 'skill_id': 'pdf', 'version': '20260709'},
        {'type': 'custom', 'skill_id': 'skill_ours', 'version': '17'},
    ],
}


def _at_snapshot(metadata: Mapping[str, str]) -> Callable[[str, int], Mapping[str, str]]:
    """The agent's metadata, readable only at the version the snapshot pinned."""

    def read(agent_id: str, version: int) -> Mapping[str, str]:
        assert (agent_id, version) == ('agent_1', 36)
        return metadata

    return read


def test_each_skill_we_publish_is_traced_to_the_commit_it_came_from() -> None:
    metadata = {'skill:classifying': f'skill_ours@17 {DIGEST} {COMMIT}', 'unrelated': 'x'}
    read = provenance.read(SNAPSHOT, _at_snapshot(metadata), OURS)
    assert (read.agent_id, read.agent_version, read.model['id']) == ('agent_1', 36, 'claude-model')
    by_id = {skill.skill_id: skill for skill in read.skills}
    assert (by_id['skill_ours'].directory, by_id['skill_ours'].commit) == ('classifying', COMMIT)
    assert by_id['pdf'].commit is None


@pytest.mark.parametrize(
    ('metadata', 'ours', 'reason'),
    [
        ({'skill:classifying': f'skill_ours@16 {DIGEST} {COMMIT}'}, OURS, 'pinned skill_ours at 17'),
        ({'skill:classifying': 'skill_ours@17'}, OURS, 'not a skill record'),
        ({}, OURS, 'holds no record of it'),
        ({'skill:renamed': f'skill_ours@17 {DIGEST} {COMMIT}'}, OURS, "published from 'renamed'"),
        ({}, {}, 'pins the custom skill skill_ours, which no declaration'),
    ],
    ids=['other version', 'malformed record', 'ours without a record', 'other directory', 'custom and not ours'],
)
def test_a_skill_that_cannot_be_traced_is_refused(
    metadata: Mapping[str, str], ours: Mapping[str, str], reason: str
) -> None:
    with pytest.raises(provenance.ProvenanceError, match=reason):
        provenance.read(SNAPSHOT, _at_snapshot(metadata), ours)


def test_a_snapshot_without_a_version_is_refused_before_any_metadata_is_read() -> None:
    with pytest.raises(provenance.ProvenanceError, match='names no id, version and skills'):
        provenance.read({'id': 'agent_1', 'skills': []}, _at_snapshot({}), OURS)


def test_a_snapshot_without_a_model_is_refused() -> None:
    snapshot = {key: value for key, value in SNAPSHOT.items() if key != 'model'}
    with pytest.raises(provenance.ProvenanceError, match='names no model'):
        provenance.read(snapshot, _at_snapshot({}), OURS)


def test_the_declarations_name_the_skills_this_repository_publishes() -> None:
    # Every directory skill a committed declaration names is one of ours; the property holds for any set.
    ours = provenance.declared_skills()
    assert ours
    assert all(directory and '/' not in directory for directory in ours.values())


def test_one_skill_published_from_two_directories_is_refused(tmp_path: pathlib.Path) -> None:
    for name, directory in (('a', 'one'), ('b', 'two')):
        (tmp_path / f'{name}.agent.yaml').write_text(
            f'name: {name}\nskills:\n  - type: custom\n    skill_id: skill_x\n    directory: {directory}\n', 'utf-8'
        )
    with pytest.raises(provenance.ProvenanceError, match='published from both'):
        provenance.declared_skills(tmp_path)
