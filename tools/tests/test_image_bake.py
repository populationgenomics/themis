"""Unit tests for the `docker buildx bake` definition generator (`tools.image_bake`)."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from tools import image_bake

_PREFIX = 'australia-southeast1-docker.pkg.dev/cpg-themis-dev/themis'
_SHA = '84f4aea92763b19cd39ee6d714e2872ccbc505dc'
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _sample_entries() -> list[dict[str, str]]:
    return [
        {
            'name': 'web',
            'context': 'apps/web',
            'file': 'apps/web/Dockerfile',
            'env': 'THEMIS_WEB_IMAGE',
        },
        {
            'name': 'auth',
            'context': '.',
            'file': 'themis/services/auth/Dockerfile',
            'env': 'THEMIS_AUTH_IMAGE',
        },
    ]


def test_parse_manifest_relativizes_dockerfile_to_context() -> None:
    specs = image_bake.parse_manifest(_sample_entries())
    assert specs == [
        image_bake.ImageSpec(
            name='web',
            context='apps/web',
            dockerfile_in_context='Dockerfile',
            env='THEMIS_WEB_IMAGE',
        ),
        image_bake.ImageSpec(
            name='auth',
            context='.',
            dockerfile_in_context='themis/services/auth/Dockerfile',
            env='THEMIS_AUTH_IMAGE',
        ),
    ]


@pytest.mark.parametrize(
    ('entries', 'match'),
    [
        ([], 'non-empty JSON array'),
        ('not-a-list', 'non-empty JSON array'),
        (['not-a-dict'], 'JSON object'),
        ([{'name': '', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_X_IMAGE'}], "non-empty string 'name'"),
        (
            [{'name': 'cache-web', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'reserved build-cache prefix',
        ),
        (
            [{'name': 'Bad_Name', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'invalid image name',
        ),
        (
            [{'name': 'web', 'context': '.', 'file': 'Dockerfile', 'env': 'bad_env'}],
            'invalid image env var',
        ),
        (
            [{'name': 'web', 'context': '.', 'file': 'Dockerfile', 'env': 'PATH'}],
            'invalid image env var',
        ),
        (
            [{'name': 'web', 'context': '.', 'file': '-', 'env': 'THEMIS_WEB_IMAGE'}],
            'normalized repo-relative path',
        ),
        (
            [{'name': 'web', 'context': 'apps/web', 'file': 'other/Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'must lie inside build context',
        ),
        (
            [{'name': 'web', 'context': 'apps/web', 'file': 'apps/web', 'env': 'THEMIS_WEB_IMAGE'}],
            'names the context directory',
        ),
        (
            [{'name': 'web', 'context': '.', 'file': '.', 'env': 'THEMIS_WEB_IMAGE'}],
            'normalized repo-relative path',
        ),
        (
            [{'name': 'web', 'context': '../escape', 'file': '../escape/Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'normalized repo-relative path',
        ),
        (
            [{'name': 'web', 'context': './apps/web', 'file': 'apps/web/Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'normalized repo-relative path',
        ),
        (
            [{'name': 'web', 'context': 'apps//web', 'file': 'apps/web/Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'normalized repo-relative path',
        ),
        (
            [
                {
                    'name': 'web',
                    'context': 'https://attacker.example/repo.git',
                    'file': 'https://attacker.example/repo.git/Dockerfile',
                    'env': 'THEMIS_WEB_IMAGE',
                }
            ],
            'normalized repo-relative path',
        ),
        (
            [{'name': 'web', 'context': 'target:other', 'file': 'target:other/Dockerfile', 'env': 'THEMIS_WEB_IMAGE'}],
            'normalized repo-relative path',
        ),
        (
            [
                {'name': 'web', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_A_IMAGE'},
                {'name': 'web', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_B_IMAGE'},
            ],
            'duplicate image name',
        ),
        (
            [
                {'name': 'a', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_A_IMAGE'},
                {'name': 'b', 'context': '.', 'file': 'Dockerfile', 'env': 'THEMIS_A_IMAGE'},
            ],
            'duplicate image env var',
        ),
    ],
)
def test_parse_manifest_rejects_invalid_entries(entries: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        image_bake.parse_manifest(entries)


def test_bake_definition_binds_registry_tags_and_cache_packages() -> None:
    specs = image_bake.parse_manifest(_sample_entries())
    bake = image_bake.bake_definition(specs, image_prefix=f'{_PREFIX}/', tag=_SHA)
    group = bake['group']
    assert isinstance(group, dict)
    assert group['default'] == {'targets': ['web', 'auth']}

    targets = bake['target']
    assert isinstance(targets, dict)
    assert set(targets) == {'web', 'auth'}
    assert targets['web']['context'] == 'apps/web'
    assert targets['web']['dockerfile'] == 'Dockerfile'
    assert targets['auth']['context'] == '.'
    assert targets['auth']['dockerfile'] == 'themis/services/auth/Dockerfile'

    for name in ('web', 'auth'):
        target = targets[name]
        assert target['tags'] == [f'australia-southeast1-docker.pkg.dev/cpg-themis-dev/themis/{name}:{_SHA}']
        assert target['platforms'] == ['linux/amd64']
        expected_cache = f'australia-southeast1-docker.pkg.dev/cpg-themis-dev/themis/cache-{name}:latest'
        assert target['cache-from'] == [f'type=registry,ref={expected_cache}']
        (cache_to,) = target['cache-to']
        assert cache_to.startswith(f'type=registry,ref={expected_cache},')
        assert 'mode=max' in cache_to
        assert 'image-manifest=true' in cache_to


@pytest.mark.parametrize(
    ('prefix', 'tag', 'match'),
    [
        ('', _SHA, 'invalid image_prefix'),
        ('   ', _SHA, 'invalid image_prefix'),
        ('gcr.io/bad space/repo', _SHA, 'invalid image_prefix'),
        ('gcr.io/proj/repo,dest=/tmp/pwn', _SHA, 'invalid image_prefix'),
        ('gcr.io/proj/repo=bad', _SHA, 'invalid image_prefix'),
        ('gcr.io/proj/repo\nEVIL=1', _SHA, 'invalid image_prefix'),
        ('registry-without-repo-path', _SHA, 'invalid image_prefix'),
        (_PREFIX, '', 'invalid image tag'),
        (_PREFIX, 'bad:tag', 'invalid image tag'),
        (_PREFIX, 'bad/tag', 'invalid image tag'),
        (_PREFIX, 'bad,tag', 'invalid image tag'),
        (_PREFIX, 'sha\nEVIL=1', 'invalid image tag'),
    ],
)
def test_bake_definition_rejects_malformed_prefix_or_tag(prefix: str, tag: str, match: str) -> None:
    specs = image_bake.parse_manifest(_sample_entries())
    with pytest.raises(ValueError, match=match):
        image_bake.bake_definition(specs, image_prefix=prefix, tag=tag)
    with pytest.raises(ValueError, match=match):
        image_bake.github_env_lines(specs, image_prefix=prefix, tag=tag)


def test_bake_and_env_reject_empty_specs() -> None:
    with pytest.raises(ValueError, match='specs must not be empty'):
        image_bake.bake_definition([], image_prefix=_PREFIX, tag=_SHA)
    with pytest.raises(ValueError, match='specs must not be empty'):
        image_bake.github_env_lines([], image_prefix=_PREFIX, tag=_SHA)


def test_cli_writes_bake_json_and_github_env_for_committed_manifest(tmp_path: pathlib.Path) -> None:
    bake_out = tmp_path / 'bake.json'
    env_out = tmp_path / 'images.env'
    manifest = _REPO_ROOT / '.github' / 'images.json'

    image_bake.main(
        [
            '--manifest',
            str(manifest),
            '--image-prefix',
            _PREFIX,
            '--tag',
            _SHA,
            '--bake-out',
            str(bake_out),
            '--env-out',
            str(env_out),
        ]
    )

    declared = json.loads(manifest.read_text('utf-8'))
    bake = json.loads(bake_out.read_text('utf-8'))
    assert bake['group']['default']['targets'] == [entry['name'] for entry in declared]
    assert bake['target']['web']['dockerfile'] == 'Dockerfile'
    assert bake['target']['web']['cache-from'] == [
        'type=registry,ref=australia-southeast1-docker.pkg.dev/cpg-themis-dev/themis/cache-web:latest'
    ]
    env_lines = env_out.read_text('utf-8').splitlines()
    assert len(env_lines) == len(declared)
    assert (
        'THEMIS_WEB_IMAGE=australia-southeast1-docker.pkg.dev/cpg-themis-dev/themis/web:'
        '84f4aea92763b19cd39ee6d714e2872ccbc505dc'
    ) in env_lines


def _require_docker_buildx() -> str:
    docker = shutil.which('docker')
    if docker is not None:
        buildx = subprocess.run([docker, 'buildx', 'version'], capture_output=True, timeout=10, check=False)  # noqa: S603
        if buildx.returncode == 0:
            return docker
    if os.environ.get('CI'):
        raise RuntimeError('docker buildx is required in CI')
    pytest.skip('needs docker buildx')


def test_generated_bake_file_parses_under_docker_buildx_bake(tmp_path: pathlib.Path) -> None:
    docker = _require_docker_buildx()

    bake_out = tmp_path / 'bake.json'
    env_out = tmp_path / 'images.env'
    manifest = _REPO_ROOT / '.github' / 'images.json'
    image_bake.main(
        [
            '--manifest',
            str(manifest),
            '--image-prefix',
            _PREFIX,
            '--tag',
            _SHA,
            '--bake-out',
            str(bake_out),
            '--env-out',
            str(env_out),
        ]
    )

    printed = subprocess.run(  # noqa: S603
        [docker, 'buildx', 'bake', '--file', str(bake_out), '--push', '--print'],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert printed.returncode == 0, printed.stderr
    evaluated = json.loads(printed.stdout)
    for entry in json.loads(manifest.read_text('utf-8')):
        outputs = evaluated['target'][entry['name']]['output']
        assert outputs in (['type=registry'], [{'type': 'image', 'push': 'true'}], [{'type': 'registry'}]), outputs


def test_cli_exits_nonzero_without_writing_outputs_on_invalid_manifest(tmp_path: pathlib.Path) -> None:
    bad_manifest = tmp_path / 'bad.json'
    bad_manifest.write_text('[]', 'utf-8')
    bake_out = tmp_path / 'bake.json'
    env_out = tmp_path / 'images.env'

    with pytest.raises(SystemExit) as exc_info:
        image_bake.main(
            [
                '--manifest',
                str(bad_manifest),
                '--image-prefix',
                _PREFIX,
                '--tag',
                _SHA,
                '--bake-out',
                str(bake_out),
                '--env-out',
                str(env_out),
            ]
        )
    assert exc_info.value.code == 1
    assert not bake_out.exists()
    assert not env_out.exists()
