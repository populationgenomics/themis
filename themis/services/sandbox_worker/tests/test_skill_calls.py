"""Every rpc a skill names is one the guest can make.

A skill's text is the model's account of the callable surface, and nothing generated holds it to the allowlist:
a skill that names an rpc the hatch refuses, or an accessor `services` does not define, sends a session into
`PERMISSION_DENIED` or an `AttributeError` that no test here would see. A skill names an rpc bare as often as
qualified — `PollFullTexts`, `Validate` — so every capitalised token in the text is read against the name of every
rpc any `themis.rpc` contract declares, and one that is an rpc's name has to be on the generated allowlist.
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

from themis.services.sandbox_worker import _generated
from themis.services.sandbox_worker.guest import services as guest_services

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_SKILLS = sorted((_REPO_ROOT / 'agents' / 'skills').glob('*/SKILL.md'))
_RPC_STUBS = _REPO_ROOT / 'themis' / 'rpc'
# An rpc name is one capitalised identifier; read every such token, and let the rpc universe decide which matter.
_CAPITALISED = re.compile(r'\b[A-Z][A-Za-z]+\b')
_ACCESSOR = re.compile(r'\bservices\.([a-z_]+)\(\)')


def _every_rpc_name() -> set[str]:
    """The name of every method of every service any `themis.rpc` contract declares, exposed or not."""
    names: set[str] = set()
    for stub in sorted(_RPC_STUBS.glob('*_pb2.py')):
        module = importlib.import_module(f'themis.rpc.{stub.stem}')
        for service in module.DESCRIPTOR.services_by_name.values():
            names.update(service.methods_by_name)
    return names


def _exposed_rpc_names() -> set[str]:
    return {method.rsplit('/', 1)[1] for method in _generated.GUEST_METHODS}


def test_there_is_a_skill_to_hold_to_the_surface() -> None:
    """Parametrising over an empty set skips every case below and reports green."""
    assert _SKILLS, 'no agents/skills/*/SKILL.md found'


@pytest.mark.parametrize('skill', _SKILLS, ids=[skill.parent.name for skill in _SKILLS])
def test_every_rpc_the_skill_names_is_agent_exposed(skill: pathlib.Path) -> None:
    universe = _every_rpc_name()
    exposed = _exposed_rpc_names()
    assert universe > exposed, 'the rpc universe holds nothing beyond the exposed set, so nothing here can fail'
    named = set(_CAPITALISED.findall(skill.read_text('utf-8'))) & universe
    assert named, f'{skill.parent.name} names no rpc — the pattern or the skill has moved'
    unexposed = sorted(named - exposed)
    assert not unexposed, f'{skill.parent.name} names rpcs the hatch does not admit: {unexposed}'


@pytest.mark.parametrize('skill', _SKILLS, ids=[skill.parent.name for skill in _SKILLS])
def test_every_accessor_the_skill_names_is_generated(skill: pathlib.Path) -> None:
    named = set(_ACCESSOR.findall(skill.read_text('utf-8')))
    assert named, f'{skill.parent.name} names no services.<name>() accessor'
    missing = sorted(name for name in named if not callable(getattr(guest_services, name, None)))
    assert not missing, f'{skill.parent.name} names accessors the guest SDK does not define: {missing}'
