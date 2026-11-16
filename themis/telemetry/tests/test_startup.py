"""Startup timing: one line at the entry point, then one per step, each naming what it timed."""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

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


def test_a_step_run_as_a_span_is_recorded_under_the_span_it_runs_in_and_logged_once_it_ends(
    caplog: pytest.LogCaptureFixture, recorded_spans: in_memory_span_exporter.InMemorySpanExporter
) -> None:
    caplog.set_level(logging.INFO, logger=startup.__name__)
    clock = startup.StartupClock()
    logged_inside: list[str] = []

    with trace.get_tracer(__name__).start_as_current_span('startup') as root, clock.span('stores opened'):
        logged_inside.extend(record.getMessage() for record in caplog.records)

    [step] = [span for span in recorded_spans.get_finished_spans() if span.name == 'stores opened']
    assert step.parent is not None
    assert step.parent.span_id == root.get_span_context().span_id
    assert not any('stores opened' in message for message in logged_inside)
    assert caplog.records[-1].getMessage().startswith('startup: stores opened took ')


def test_a_step_that_raises_logs_no_line_for_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=startup.__name__)
    clock = startup.StartupClock()

    with pytest.raises(RuntimeError), clock.span('stores opened'):
        raise RuntimeError('the bucket is gone')

    assert not any('stores opened' in record.getMessage() for record in caplog.records)


def test_a_start_that_exits_marks_its_step_and_its_root_failed(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    # A start fails loud with SystemExit, which OpenTelemetry alone does not mark a span failed for.
    clock = startup.StartupClock()

    with pytest.raises(SystemExit), startup.trace_root(), clock.span('stores opened'):
        raise SystemExit('the resources bucket lacks a pinned build')

    statuses = {span.name: span.status.status_code for span in recorded_spans.get_finished_spans()}
    assert statuses == {'stores opened': trace.StatusCode.ERROR, startup.ROOT_SPAN: trace.StatusCode.ERROR}
