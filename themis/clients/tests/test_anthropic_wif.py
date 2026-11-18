"""The federation credential: the identity token is minted for the Claude API audience, and building it is offline."""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.parse

import httpx2
import pytest
import requests
import requests.adapters
from anthropic.lib import credentials as anthropic_credentials

from themis.clients import anthropic_wif


def _jwt(exp: int) -> str:
    def segment(payload: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')

    return f'{segment({"alg": "RS256"})}.{segment({"exp": exp})}.c2lnbmF0dXJl'


def _metadata_response(status: int, body: bytes, *, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update({'Metadata-Flavor': 'Google', 'Content-Type': 'text/plain', **(headers or {})})
    response.raw = io.BytesIO(body)
    return response


def test_the_identity_token_is_minted_for_the_claude_api(monkeypatch: pytest.MonkeyPatch) -> None:
    # The audience is half of what a federation rule matches on, and the only input to the exchange that
    # no environment variable carries — a wrong one is refused at the exchange, not at deploy.
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    minted = _jwt(int(time.time()) + 3600)
    identity_queries: list[dict[str, list[str]]] = []
    exchanges: list[tuple[str, dict[str, object]]] = []

    def metadata_send(_adapter: object, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        assert request.headers.get('Metadata-Flavor') == 'Google'
        parsed = urllib.parse.urlparse(request.url or '')
        if parsed.path in ('', '/'):
            return _metadata_response(200, b'')
        if parsed.path == '/computeMetadata/v1/instance/service-accounts/default/':
            return _metadata_response(
                200,
                json.dumps({'email': 'sa@themis.iam.gserviceaccount.com'}).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            )
        assert parsed.path == '/computeMetadata/v1/instance/service-accounts/default/identity'
        identity_queries.append(urllib.parse.parse_qs(parsed.query))
        return _metadata_response(200, minted.encode('utf-8'))

    def anthropic_send(_transport: object, request: httpx2.Request) -> httpx2.Response:
        exchanges.append((str(request.url), json.loads(request.content.decode('utf-8'))))
        return httpx2.Response(
            200,
            json={'access_token': 'sk-ant-oat-test', 'token_type': 'Bearer', 'expires_in': 3600},
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', metadata_send)
    monkeypatch.setattr(httpx2.HTTPTransport, 'handle_request', anthropic_send)

    with anthropic_wif.credentials(
        federation_rule_id='fdrl_rule',
        organization_id='00000000-0000-4000-8000-000000000000',
        service_account_id='svac_account',
        workspace_id='wrkspc_workspace',
    ) as credential:
        token = credential()

    assert token.token == 'sk-ant-oat-test'
    assert identity_queries == [{'audience': ['https://api.anthropic.com'], 'format': ['full']}]
    assert exchanges == [
        (
            'https://api.anthropic.com/v1/oauth/token',
            {
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': minted,
                'federation_rule_id': 'fdrl_rule',
                'organization_id': '00000000-0000-4000-8000-000000000000',
                'service_account_id': 'svac_account',
                'workspace_id': 'wrkspc_workspace',
            },
        )
    ]


def test_building_the_credential_mints_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Construction is offline by contract: the first exchange happens on the first request. A metadata
    # server reached at construction would fail every test and every startup outside Cloud Run.
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)

    def no_http(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('HTTP transport reached during credential construction')

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', no_http)
    monkeypatch.setattr(httpx2.HTTPTransport, 'handle_request', no_http)

    with anthropic_wif.credentials(
        federation_rule_id='fdrl_test',
        organization_id='00000000-0000-4000-8000-000000000000',
        service_account_id='svac_test',
        workspace_id='wrkspc_test',
    ) as credential:
        assert isinstance(credential, anthropic_credentials.WorkloadIdentityCredentials)
