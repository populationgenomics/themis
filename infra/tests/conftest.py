"""One mocked run of the program per test session: Pulumi's mock runtime is process-global."""

from __future__ import annotations

import pytest

from themis_infra import capture


@pytest.fixture(scope='session')
def program() -> capture.Capture:
    return capture.capture_program()
