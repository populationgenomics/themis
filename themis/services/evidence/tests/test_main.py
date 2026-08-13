"""The image's composition: every interface in the tree is in the entrypoint's registry."""

from __future__ import annotations

import importlib
import pathlib

from themis.services import evidence
from themis.services.evidence import __main__ as main_mod


def _interface_names() -> set[str]:
    # Found by the file that registers, not by __init__.py: a subpackage without one still imports (an
    # implicit namespace package inside a regular parent) and would be invisible to a pkgutil scan.
    root = pathlib.Path(evidence.__path__[0])
    return {child.name for child in root.iterdir() if (child / 'interface.py').is_file()}


def test_every_interface_is_registered() -> None:
    # An interface absent from INTERFACES imports, type-checks and passes its own tests, yet answers
    # UNIMPLEMENTED in the deployed image.
    names = _interface_names()
    assert names  # a tree with no interface at all would pass vacuously
    missing = sorted(
        name
        for name in names
        if importlib.import_module(f'{evidence.__name__}.{name}.interface').register not in main_mod.INTERFACES
    )
    assert not missing, f'interfaces absent from the entrypoint registry: {missing}'


def test_every_registration_comes_from_an_interface_module() -> None:
    # The check above finds interfaces by that filename, so a `register` housed anywhere else is
    # invisible to it. Holding the convention is what keeps that discovery sound.
    expected = {f'{evidence.__name__}.{name}.interface' for name in _interface_names()}
    stray = sorted(register.__module__ for register in main_mod.INTERFACES if register.__module__ not in expected)
    assert not stray, f'registrations that are not an interface.py of this image: {stray}'
