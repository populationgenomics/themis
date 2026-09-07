"""The metric the exporter writes is the one the infrastructure declares.

The Pulumi program runs in its own interpreter and imports nothing from `themis`, and the exporter imports nothing from
the program, so each names the metric and its label on its own; this holds the two together, the way
`tests/test_images_manifest.py` holds the image overrides to the program.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from themis.services.cost_exporter import gauge

_COST_INFRA = pathlib.Path(__file__).resolve().parents[4] / 'infra' / 'themis_infra' / 'cost.py'


def _declared(constant: str) -> str:
    """The string literal `cost.py` assigns to a module constant."""
    source = _COST_INFRA.read_text('utf-8')
    matches = re.findall(rf"^{constant} = '([^']*)'$", source, re.MULTILINE)
    if len(matches) != 1:
        pytest.fail(f'{_COST_INFRA.name} assigns {constant} {len(matches)} times; expected one string literal')
    return matches[0]


def test_the_infrastructure_declares_the_metric_the_exporter_writes() -> None:
    assert _declared('_METRIC_TYPE') == gauge.METRIC_TYPE
    assert _declared('_AGENT_LABEL') == gauge.AGENT_LABEL
