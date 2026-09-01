"""The generated stub accessors: one per agent-exposed service, each over the guest's hatch channel.

The accessor set is generated from the option the hatch allowlist is generated from, so the two cannot disagree
about what is exposed — `test_hatch.py` asserts that correspondence. What generation does not settle is whether
the emitted module *works*: an accessor that constructs its stub on some other channel would reach past the
hatch, and one naming a stub class its module does not define would fail at import — both inside a session,
where no test runs.
"""

from __future__ import annotations

import inspect

import pytest

from themis.services.sandbox_worker import _generated
from themis.services.sandbox_worker.guest import services

_ACCESSORS = [name for name, value in vars(services).items() if inspect.isfunction(value)]


def test_every_accessor_reaches_the_hatch_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no hatch socket there is no channel to build a stub on, so every accessor has to fail.

    An accessor that returned a stub here would have dialled something other than the hatch — the one exit the
    sandbox has. Constructing each one also proves the generator named a stub class that exists.
    """
    monkeypatch.delenv('POSTERN_HATCH', raising=False)
    assert _ACCESSORS, 'no accessors found on the generated module'
    for name in _ACCESSORS:
        with pytest.raises(KeyError, match='POSTERN_HATCH'):
            getattr(services, name)()


def test_every_allowlisted_method_is_callable_off_its_accessor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each accessor hands back the stub for *its* service, so the allowlist and the SDK name one surface.

    An accessor paired with another service's stub satisfies both existence and the hatch check above while
    leaving every rpc the agent was given uncallable.
    """
    monkeypatch.setenv('POSTERN_HATCH', '/nonexistent/hatch')  # dialling is lazy, so no server has to answer
    assert _generated.GUEST_METHODS, 'no methods parsed out of the generated allowlist'
    for method in sorted(_generated.GUEST_METHODS):
        package, rpc = method.lstrip('/').split('/')
        stub = getattr(services, package.rsplit('.', 2)[-2])()
        assert callable(getattr(stub, rpc, None)), f'{method} is allowlisted but not callable off its accessor'
