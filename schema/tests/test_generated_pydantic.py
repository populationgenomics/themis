"""Validate the committed Pydantic models under schema/tests/pydantic/.

Guards the S0.2 invariants without the Node toolchain or datamodel-code-generator:
every committed model file imports cleanly (proving the normalize -> Pydantic recipe
produced valid v2 models) and the spurious ``RootModel[Any]`` root is gone.

The ``test_features_*`` cases are the Pydantic half of the S0.5 round-trip verification:
each construct in the feature-coverage corpus (``schema/tests/fixtures/features/``) must
survive to a usable model. The JSON Schema half lives in ``test_committed_schemas``; the
Zod half is the ``tsc`` smoke test. Freshness against the ``.tsp`` sources is the S0.4 CI
gate; these run in the ordinary pytest job.
"""

from __future__ import annotations

import datetime
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


@pytest.fixture
def features() -> types.ModuleType:
    return _load(_PYDANTIC_DIR / 'features.py')


def test_features_string_enum_is_specialized_and_closed(features: types.ModuleType) -> None:
    # --use-specialized-enum: StrEnum, not a bare Enum; the value set is closed.
    assert issubclass(features.Colour, enum.StrEnum)
    assert features.EnumHolder(colour='red').colour == 'red'
    with pytest.raises(pydantic.ValidationError):
        features.EnumHolder(colour='chartreuse')


def test_features_optional_field_defaults_to_none(features: types.ModuleType) -> None:
    holder = features.OptionalHolder(required_field='x')
    assert holder.optional_field is None
    assert holder.model_dump(mode='json') == {'required_field': 'x', 'optional_field': None}
    with pytest.raises(pydantic.ValidationError):
        features.OptionalHolder()  # required_field is not optional


def test_features_optional_with_default_carries_the_default(features: types.ModuleType) -> None:
    assert features.DefaultHolder().flagged is False
    assert features.DefaultHolder(flagged=True).flagged is True


def test_features_literal_pins_a_single_value(features: types.ModuleType) -> None:
    assert features.LiteralHolder(kind='widget').kind == 'widget'
    with pytest.raises(pydantic.ValidationError):
        features.LiteralHolder(kind='gadget')


def test_features_named_union_enforces_the_per_variant_rule(features: types.ModuleType) -> None:
    # The licensed variant requires `publisher`; the union rejects it absent —
    # the cross-field "iff" rule holding structurally, no hand-written validator.
    licensed = features.AccessHolder.model_validate({'access': {'access': 'licensed', 'publisher': 'acme'}})
    assert licensed.access.root.publisher == 'acme'
    free = features.AccessHolder.model_validate({'access': {'access': 'free-to-read'}})
    assert free.access.root.access == 'free-to-read'
    with pytest.raises(pydantic.ValidationError):
        features.AccessHolder.model_validate({'access': {'access': 'licensed'}})


def test_features_nested_model_round_trips(features: types.ModuleType) -> None:
    outer = features.Outer(inner=features.Inner(value='v'))
    dumped = outer.model_dump(mode='json')
    assert dumped == {'inner': {'value': 'v'}}
    assert features.Outer.model_validate(dumped) == outer


def test_features_arrays_round_trip(features: types.ModuleType) -> None:
    holder = features.ArrayHolder(tags=['a', 'b'], palette=[features.Colour.red, features.Colour.blue])
    dumped = holder.model_dump(mode='json')
    assert dumped == {'tags': ['a', 'b'], 'palette': ['red', 'blue']}
    assert features.ArrayHolder.model_validate(dumped) == holder


def test_features_scalar_formats_round_trip(features: types.ModuleType) -> None:
    holder = features.ScalarHolder(
        count=7,
        ratio=1.5,
        when=datetime.datetime(2026, 6, 20, 9, 30, tzinfo=datetime.UTC),
        day=datetime.date(2026, 6, 20),
        link='https://example.org/paper',
    )
    dumped = holder.model_dump(mode='json')
    assert dumped['when'] == '2026-06-20T09:30:00Z'
    assert dumped['day'] == '2026-06-20'
    assert features.ScalarHolder.model_validate(dumped) == holder
    # int32 carries its bounds; an out-of-range value is rejected.
    with pytest.raises(pydantic.ValidationError):
        features.ScalarHolder.model_validate({**dumped, 'count': 2**31})
