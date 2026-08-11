"""`.github/images.json` is the single declaration of the service image set.

`deploy.yml` builds and pushes from it; `images.yml` build-checks the same list on merge.
Neither notices an entry that is missing or names the wrong `env`: an unlisted Dockerfile
is simply never built, and a misspelt `env` leaves the real override unset, which the
Pulumi program resolves to the service's live image — a green deploy of the previous
commit either way. These tests are the only thing that fails on that.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_IMAGES_JSON = _REPO_ROOT / '.github' / 'images.json'
_INFRA_MAIN = _REPO_ROOT / 'infra' / '__main__.py'
_IMAGE_ENV = re.compile(r'THEMIS_[A-Z_]+_IMAGE')

# `images.json` declares the *service* image set: every entry carries the Pulumi override
# the program reads, which is what `test_declared_envs_are_the_overrides_the_program_reads`
# pins. Images under `tools/` are operator tooling with no deployed service behind them —
# the Dataflow worker reaches its job through `--sdk-image`, not an override — so they have
# no env to declare and are built out of band.
_UNDEPLOYED_PREFIX = 'tools/'


def _declared() -> list[dict[str, str]]:
    return json.loads(_IMAGES_JSON.read_text('utf-8'))


def _tracked_dockerfiles() -> set[str]:
    listed = subprocess.run(
        ['git', 'ls-files', '*Dockerfile'],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {f for f in listed.stdout.split() if not f.startswith(_UNDEPLOYED_PREFIX)}


def test_every_tracked_dockerfile_is_declared() -> None:
    declared = {entry['file'] for entry in _declared()}
    assert declared, 'images.json declares no images'
    assert declared == _tracked_dockerfiles()


def test_declared_envs_are_the_overrides_the_program_reads() -> None:
    # The program names each override once, as a module constant; an entry whose `env`
    # is not among them silently deploys the live image instead of the one just built.
    assert {entry['env'] for entry in _declared()} == set(_IMAGE_ENV.findall(_INFRA_MAIN.read_text('utf-8')))


def test_declared_build_inputs_exist() -> None:
    for entry in _declared():
        assert (_REPO_ROOT / entry['file']).is_file(), entry['file']
        assert (_REPO_ROOT / entry['context']).is_dir(), entry['context']


def test_image_names_are_unique() -> None:
    # The name is the Artifact Registry repository path, so a duplicate would have two
    # services overwriting each other's tag at the same commit.
    names = [entry['name'] for entry in _declared()]
    assert len(names) == len(set(names))
