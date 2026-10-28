"""The Dataflow worker image runs the Beam SDK the launcher submits with.

The launcher submits jobs with the `apache-beam` the `dataflow` dependency group pins, and the worker starts its
harness through the bootloader `tools/litcache/Dockerfile` copies out of the official SDK image. A mismatch surfaces
only as workers failing to start, so the image tag is held to the pin here.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PIN = re.compile(r'apache-beam\[gcp\]==(?P<version>\S+)')
_SDK_IMAGE = re.compile(r'^FROM apache/beam_python3\.13_sdk:(?P<version>\S+) AS beam$', re.MULTILINE)


def _pinned_beam() -> str:
    groups = tomllib.loads((_REPO_ROOT / 'pyproject.toml').read_text('utf-8'))['dependency-groups']
    pins = [m['version'] for req in groups['dataflow'] if isinstance(req, str) and (m := _PIN.fullmatch(req))]
    if len(pins) != 1:
        raise ValueError(f'expected one exact apache-beam[gcp] pin in the dataflow group, found {pins}')
    return pins[0]


def _sdk_image_tag() -> str:
    tags = _SDK_IMAGE.findall((_REPO_ROOT / 'tools' / 'litcache' / 'Dockerfile').read_text('utf-8'))
    if len(tags) != 1:
        raise ValueError(f'expected one beam_python3.13_sdk stage in tools/litcache/Dockerfile, found {tags}')
    return tags[0]


def test_the_worker_image_grafts_the_pinned_beam_sdk() -> None:
    assert _sdk_image_tag() == _pinned_beam()
