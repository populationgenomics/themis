"""Every stack file satisfies the program, and each opt-in is on in dev and off in prod."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from themis_infra import capture

_INFRA = pathlib.Path(__file__).resolve().parents[1]


def test_the_project_has_stack_files() -> None:
    assert capture.stack_files()


@pytest.mark.parametrize('stack_file', capture.stack_files(), ids=lambda path: path.name)
def test_the_program_runs_against_every_stack_file(stack_file: pathlib.Path) -> None:
    # Pulumi's mock runtime is process-global and the session fixture holds dev's run, so each stack gets a process.
    stack = stack_file.name.removeprefix('Pulumi.').removesuffix('.yaml')
    result = subprocess.run(  # noqa: S603 — fixed argv: this interpreter, our module, a stack name from our files
        [sys.executable, '-m', 'themis_infra.capture', stack],
        cwd=_INFRA,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]


@pytest.mark.parametrize('key', sorted(capture.OPT_INS))
def test_dev_turns_on_every_opt_in(key: str) -> None:
    # The grants rule and the diagrams read dev's run, so a grant behind an opt-in dev leaves off goes unchecked.
    assert capture.stack_config('dev')[key] == 'true'


@pytest.mark.parametrize('key', sorted(capture.OPT_INS))
def test_prod_keeps_every_opt_in_off(key: str) -> None:
    assert capture.stack_config('prod')[key] == 'false'


@pytest.mark.parametrize('value', ['true', 'false', 'True', 'False', True, False])
def test_an_unclassified_switch_is_refused(value: str | bool) -> None:
    with pytest.raises(ValueError, match='themis:enableSomethingNew'):
        capture.check_switches_classified({'themis:enableSomethingNew': value}, 'Pulumi.test.yaml')


def test_a_value_that_is_not_a_switch_needs_no_class() -> None:
    capture.check_switches_classified(
        {'themis:sqlTier': 'db-f1-micro', 'themis:authMaxInstances': '3'}, 'Pulumi.test.yaml'
    )


def test_structured_config_outside_the_object_keys_is_refused() -> None:
    # A misspelt `secure` envelope would otherwise reach the program as an object instead of a secret.
    with pytest.raises(ValueError, match='themis:key: unexpected structured config'):
        capture.config_values({'themis:key': {'secur': 'v1:x'}}, 'Pulumi.test.yaml')


def test_an_object_key_reaches_the_program_as_json() -> None:
    key = sorted(capture.OBJECT_KEYS)[0]
    values = capture.config_values({key: {'a': 'b'}, 'themis:token': {'secure': 'v1:x'}}, 'Pulumi.test.yaml')
    assert values == {key: '{"a": "b"}', 'themis:token': 'mock-secret'}
