"""The operational vocabulary the producers write under is the one the alert policies read — and nothing else.

The Pulumi program imports nothing from `themis`, and the producers nothing from the program, so each names the
metrics and labels on its own; this holds the two together both ways, as `test_metric_contract.py` does for the spend
vocabulary. The program's module is loaded by path, not through its package, whose `__init__` imports the Pulumi SDK.
"""

from __future__ import annotations

import importlib.util
import pathlib

from themis.telemetry import operational_names

_OPERATIONAL_METRICS = pathlib.Path(__file__).resolve().parents[3] / 'infra' / 'themis_infra' / 'operational_metrics.py'


def _infra_vocabulary() -> dict[str, object]:
    spec = importlib.util.spec_from_file_location('operational_metrics', _OPERATIONAL_METRICS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: value for name, value in vars(module).items() if name.isupper()}


def _producer_vocabulary() -> dict[str, object]:
    vocabulary: dict[str, object] = {
        name: value for name, value in vars(operational_names).items() if name.isupper() and isinstance(value, str)
    }
    # The policies name only the success of each outcome label; every other value counts as a failure.
    vocabulary['ACL_LOADED'] = operational_names.AclLoadOutcome.LOADED.value
    vocabulary['DEPLOY_SUCCEEDED'] = operational_names.DeployResult.SUCCEEDED.value
    return vocabulary


def test_the_infrastructure_names_exactly_what_the_producers_write() -> None:
    vocabulary = _producer_vocabulary()
    assert len(vocabulary) > 1  # a vocabulary of nothing would hold trivially

    assert _infra_vocabulary() == vocabulary
