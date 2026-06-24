"""Validate the committed Pydantic models under schema/tests/pydantic/.

Guards the S0.2 invariants without the Node toolchain or datamodel-code-generator:
every committed model file imports cleanly (proving the normalize -> Pydantic recipe
produced valid v2 models), the spurious ``RootModel[Any]`` root is gone, and the demo
domain round-trips a sample instance. Freshness against the ``.tsp`` sources is a
separate CI gate (S0.4); these run in the ordinary pytest job.
"""

from __future__ import annotations

import enum
import functools
import importlib.util
import pathlib
import sys
import types

import pydantic
import pytest

_PYDANTIC_DIR = pathlib.Path(__file__).resolve().parent / 'pydantic'


def _committed_models() -> list[pathlib.Path]:
    return sorted(_PYDANTIC_DIR.glob('*.py'))


@functools.cache
def _load(path: pathlib.Path) -> types.ModuleType:
    # Cached: each generated file is exec'd once per session. Without it every
    # test reloads its model and clobbers the sys.modules entry below.
    module_name = f'_generated_{path.stem}'
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the models use `from __future__ import annotations`,
    # so pydantic resolves the deferred forward refs (e.g. `colour: Colour`) via
    # sys.modules[__name__].__dict__ — absent that, the class stays "not fully
    # defined".
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_at_least_one_model_committed() -> None:
    assert _committed_models(), f'no *.py under {_PYDANTIC_DIR}'


@pytest.mark.parametrize('model_path', _committed_models(), ids=lambda p: p.name)
def test_module_imports(model_path: pathlib.Path) -> None:
    _load(model_path)


@pytest.mark.parametrize('model_path', _committed_models(), ids=lambda p: p.name)
def test_no_spurious_root_model(model_path: pathlib.Path) -> None:
    module = _load(model_path)
    # regen passes --skip-root-model, so the bundle root's meaningless
    # RootModel[Any] is never emitted; nothing named Model should survive.
    assert not hasattr(module, 'Model'), 'spurious RootModel[Any] root was emitted'


def test_demo_colour_is_a_specialized_enum() -> None:
    demo = _load(_PYDANTIC_DIR / 'demo.py')
    # --use-specialized-enum: StrEnum, not a bare Enum.
    assert issubclass(demo.Colour, enum.StrEnum)


def test_demo_round_trips_a_sample_instance() -> None:
    demo = _load(_PYDANTIC_DIR / 'demo.py')
    catalogue = demo.Catalogue(widgets=[demo.Widget(name='left-handed', colour=demo.Colour.red)])
    dumped = catalogue.model_dump(mode='json')
    assert dumped == {'widgets': [{'name': 'left-handed', 'colour': 'red'}]}
    assert demo.Catalogue.model_validate(dumped) == catalogue


def test_demo_rejects_unknown_enum_member() -> None:
    demo = _load(_PYDANTIC_DIR / 'demo.py')
    with pytest.raises(pydantic.ValidationError):
        demo.Widget(name='x', colour='chartreuse')
