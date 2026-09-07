"""The federation credential: the identity token is minted for the Claude API audience, and building it is offline."""

from __future__ import annotations

import pytest
from anthropic.lib import credentials as anthropic_credentials

from themis.clients import anthropic_wif


def test_the_identity_token_is_minted_for_the_claude_api(monkeypatch: pytest.MonkeyPatch) -> None:
    # The audience is half of what a federation rule matches on, and the only input to the exchange that
    # no environment variable carries — a wrong one is refused at the exchange, not at deploy.
    seen: list[str] = []

    def fetch_id_token(_request: object, audience: str) -> str:
        seen.append(audience)
        return 'header.payload.signature'

    monkeypatch.setattr(anthropic_wif.id_token, 'fetch_id_token', fetch_id_token)

    assert anthropic_wif.identity_token() == 'header.payload.signature'
    assert seen == ['https://api.anthropic.com']


def test_building_the_credential_mints_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Construction is offline by contract: the first exchange happens on the first request. A metadata
    # server reached at construction would fail every test and every startup outside Cloud Run.
    def refuse() -> str:
        raise AssertionError('the identity token was minted at construction')

    monkeypatch.setattr(anthropic_wif, 'identity_token', refuse)

    with anthropic_wif.credentials(
        federation_rule_id='fdrl_test',
        organization_id='00000000-0000-4000-8000-000000000000',
        service_account_id='svac_test',
        workspace_id='wrkspc_test',
    ) as credential:
        assert isinstance(credential, anthropic_credentials.WorkloadIdentityCredentials)
