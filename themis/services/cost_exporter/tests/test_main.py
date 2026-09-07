"""The entrypoint's environment contract: every setting required, each miss named, the deadline a whole second count."""

from __future__ import annotations

import pytest

from themis.services.cost_exporter import __main__ as main_mod

_ENV = {
    'ANTHROPIC_FEDERATION_RULE_ID': 'fdrl_test',
    'ANTHROPIC_ORGANIZATION_ID': '00000000-0000-4000-8000-000000000000',
    'ANTHROPIC_SERVICE_ACCOUNT_ID': 'svac_test',
    'ANTHROPIC_WORKSPACE_ID': 'wrkspc_test',
    'THEMIS_COST_EXPORTER_PROJECT': 'themis-test',
    'THEMIS_COST_EXPORTER_LOCATION': 'australia-southeast1',
    'THEMIS_COST_EXPORTER_DEADLINE_SECONDS': '180',
}


def test_a_complete_environment_reads_as_the_settings() -> None:
    assert main_mod.settings_from(_ENV) == main_mod.Settings(
        federation_rule_id='fdrl_test',
        organization_id='00000000-0000-4000-8000-000000000000',
        service_account_id='svac_test',
        workspace_id='wrkspc_test',
        project='themis-test',
        location='australia-southeast1',
        deadline_seconds=180,
    )


@pytest.mark.parametrize('missing', sorted(_ENV))
def test_a_missing_variable_exits_naming_it(missing: str) -> None:
    environ = {name: value for name, value in _ENV.items() if name != missing}
    with pytest.raises(SystemExit, match=missing):
        main_mod.settings_from(environ)


@pytest.mark.parametrize('deadline', ['0', '-5', '1.5', 'soon'])
def test_a_deadline_that_is_not_a_positive_second_count_exits(deadline: str) -> None:
    with pytest.raises(SystemExit, match='positive whole number of seconds'):
        main_mod.settings_from({**_ENV, 'THEMIS_COST_EXPORTER_DEADLINE_SECONDS': deadline})
