"""Startup timing: one line at the entry point, then one per step, each naming what it timed."""

from __future__ import annotations

import logging

import pytest

from themis.telemetry import startup


def test_each_step_logs_a_line_naming_it_after_the_entry_point_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=startup.__name__)
    clock = startup.StartupClock()
    clock.step('backend built')
    clock.step('server started')

    messages = [record.getMessage() for record in caplog.records if record.name == startup.__name__]
    assert len(messages) == 3
    assert messages[0].startswith('startup: entry point reached')
    for message, name in zip(messages[1:], ('backend built', 'server started'), strict=True):
        assert message.startswith(f'startup: {name} took ')
