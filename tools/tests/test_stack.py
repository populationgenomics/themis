from __future__ import annotations

import pathlib

import pytest

from tools import clu, stack, web_app


def _stack(infra: pathlib.Path, name: str, project: str, domain: str | None) -> None:
    lines = ['config:', f'  gcp:project: {project}']
    if domain is not None:
        lines.append(f'  themis:domain: {domain}')
    (infra / f'Pulumi.{name}.yaml').write_text('\n'.join(lines) + '\n', 'utf-8')


def test_a_value_is_read_from_the_stack_deployed_in_the_project(tmp_path: pathlib.Path) -> None:
    _stack(tmp_path, 'dev', 'proj-dev', 'app-dev.example.org')
    _stack(tmp_path, 'prod', 'proj-prod', 'app.example.org')
    assert stack.value('proj-dev', 'themis:domain', infra_dir=tmp_path) == 'app-dev.example.org'


@pytest.mark.parametrize(
    ('stacks', 'match'),
    [
        ([], 'found'),
        ([('a', 'proj', 'one.example.org'), ('b', 'proj', 'two.example.org')], 'found'),
        ([('dev', 'proj', None)], 'sets no themis:domain'),
    ],
    ids=['no stack', 'two stacks', 'no domain'],
)
def test_a_project_without_exactly_one_stack_setting_the_value_is_refused(
    tmp_path: pathlib.Path, stacks: list[tuple[str, str, str | None]], match: str
) -> None:
    for name, project, domain in stacks:
        _stack(tmp_path, name, project, domain)
    with pytest.raises(ValueError, match=match):
        stack.value('proj', 'themis:domain', infra_dir=tmp_path)


def test_the_committed_default_project_names_its_app() -> None:
    assert web_app.origin(clu.DEFAULT_PROJECT).startswith('https://')
